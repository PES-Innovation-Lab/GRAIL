# TODO:
# Add VF2/3 to the pipeline
# Clean up functions

# Imports

import sys
import time

import faiss
import networkx as nx
import pymetis
import torch
import torch.nn.functional as F
import vf3py
from torch.nn import Dropout, LeakyReLU, Linear, ReLU, Sequential
from torch_geometric.datasets import CoraFull
from torch_geometric.nn import GINConv, global_mean_pool
from torch_geometric.utils import k_hop_subgraph, to_networkx

# Load dataset

DATASET = CoraFull(root="/tmp/Cora")[0]

print("Cora Dataset")
print("=" * 80)
print(f"Number of nodes: {DATASET.num_nodes}")
print(f"Number of edges: {DATASET.num_edges}")
print(f"Number of features: {DATASET.num_node_features}")
print("=" * 80)


# Triplet Loss
# max(0, || zq - zpos || ^ 2 - || zq - zneg || ^ 2 + alpha)
#
# Change alpha to increase margin between positive samples and negative samples
def triplet_loss(zq, zpos, zneg, alpha=1.0):
    dist_pos = F.pairwise_distance(zq, zpos, p=2)
    dist_neg = F.pairwise_distance(zq, zneg, p=2)
    loss = F.relu(dist_pos - dist_neg + alpha)
    return loss.mean()


# Generate triplets
# 1. Gq, which is sampled from Gpos
# 2. Gpos, from which Gq is sampled
# 3. Gneg, which is not a match for Gq
#
# k: k-hops to generate Gpos and Gneg
# q_size: max number of nodes in Gq
def generate_triplets(data, k=4, q_size=80):
    anchor_node = torch.randint(0, data.num_nodes, (1,)).item()
    subset_pos, _, _, _ = k_hop_subgraph(
        anchor_node, k, data.edge_index, relabel_nodes=True
    )

    Gpos = data.subgraph(subset_pos)

    q_nodes = subset_pos[torch.randperm(len(subset_pos))][:q_size]
    Gq = data.subgraph(q_nodes)

    neg_anchor = torch.randint(0, data.num_nodes, (1,)).item()
    attempts = 0
    while neg_anchor == anchor_node and attempts < 5:
        neg_anchor = torch.randint(0, data.num_nodes, (1,)).item()
        attempts += 1

    subset_neg, _, _, _ = k_hop_subgraph(
        neg_anchor, k, data.edge_index, relabel_nodes=True
    )
    Gneg = data.subgraph(subset_neg)

    return Gq, Gpos, Gneg


# Model
class SubgraphEncoder(torch.nn.Module):
    def __init__(self, in_neurons, hidden_neurons, output_neurons):
        super().__init__()
        nn1 = Sequential(
            Linear(in_neurons, hidden_neurons),
            Dropout(0.1),
            LeakyReLU(1.5),
            Linear(hidden_neurons, hidden_neurons),
            ReLU(),
            Linear(hidden_neurons, hidden_neurons),
            ReLU(),
            Linear(hidden_neurons, hidden_neurons),
        )
        self.conv1 = GINConv(nn1)
        self.lin = Linear(hidden_neurons, output_neurons)

    def forward(self, x, edge_index, batch):
        h = self.conv1(x, edge_index)
        h = global_mean_pool(h, batch)
        return F.normalize(self.lin(h), dim=1)


device = torch.device("cuda")

encoder = SubgraphEncoder(
    in_neurons=DATASET.num_features, hidden_neurons=64, output_neurons=16
)
optimizer = torch.optim.Adam(encoder.parameters(), lr=1e-3)

encoder = encoder.to(device)
DATASET = DATASET.to(device)

epochs = 280
steps_per_epoch = 512


# Training
def train(encoder, dataset, epochs, steps_per_epoch, device):

    for epoch in range(epochs):
        total_loss = 0

        for _ in range(steps_per_epoch):

            result = generate_triplets(dataset)
            if not result:
                continue

            Gq, Gpos, Gneg = result
            Gq.batch = torch.zeros(Gq.num_nodes, dtype=torch.long, device=device)
            Gpos.batch = torch.zeros(Gpos.num_nodes, dtype=torch.long, device=device)
            Gneg.batch = torch.zeros(Gneg.num_nodes, dtype=torch.long, device=device)

            zq = encoder(Gq.x, Gq.edge_index, Gq.batch)
            zpos = encoder(Gpos.x, Gpos.edge_index, Gpos.batch)
            zneg = encoder(Gneg.x, Gneg.edge_index, Gneg.batch)

            loss = triplet_loss(
                zq.unsqueeze(0), zpos.unsqueeze(0), zneg.unsqueeze(0), alpha=1.0
            )

            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

            total_loss += loss.item()

        print(f"Epoch {epoch}: Triplet Loss = {total_loss:.4f}")

    torch.save(encoder.state_dict(), "cora-model.pth")


# Uncomment to re-train
# train(encoder, DATASET, epochs, steps_per_epoch, device)


# Load saved model
encoder.load_state_dict(torch.load("cora-model.pth"))
encoder.eval()


# Generate embedding for a single graph
def get_graph_embedding(dataset):
    with torch.no_grad():
        dataset.batch = torch.zeros(dataset.num_nodes, dtype=torch.long, device=device)
        z = encoder(dataset.x, dataset.edge_index, dataset.batch)

    return z


# Partition a graph using METIS
def make_partitions(dataset, num_parts):
    part_dict = {}
    for i in range(num_parts):
        part_dict[i] = []

    part_graphs = []

    nx_graph = to_networkx(dataset)
    adjacency_list = {i: list(nx_graph.neighbors(i)) for i in nx_graph.nodes}
    _, membership = pymetis.part_graph(num_parts, adjacency_list)

    for i in range(len(membership)):
        part_dict[membership[i]].append(i)

    for i in range(num_parts):
        part_graphs.append(dataset.subgraph(torch.tensor(part_dict[i]).to(device)))
        # print(f"Partition {i}:", len(part_dict[i]))

    return part_graphs


def main():

    args = sys.argv[1:]
    partition_num = 0

    if len(args) == 1:
        partition_num = int(args[0])
    elif len(args) > 1:
        print("Wrong Usage: python3 cora_pipeline.py <Partition Number>")

    # Build vector index using FAISS
    index = faiss.IndexFlatL2(16)

    # Make partitions
    k = 40
    partition_list = make_partitions(DATASET, k)

    # Generate embeddings for each partition
    partition_embeddings = []

    for i in range(len(partition_list)):
        emb = get_graph_embedding(partition_list[i])
        partition_embeddings.append(emb)
        index.add(emb.cpu())

    # Sample query
    # Generate a query graph from Partition "partition_num", so we know it will always be an exact match
    Gq, Gpos, Gneg = generate_triplets(partition_list[partition_num])
    # Generate query embedding
    model_start_time = time.time()
    zq = get_graph_embedding(Gq)
    model_end_time = time.time()

    # Search for query embedding within FAISS index
    faiss_start_time = time.time()
    D, I = index.search(zq.cpu(), k)
    faiss_end_time = time.time()

    model_time = (model_end_time - model_start_time) * 1000
    faiss_time = (faiss_end_time - faiss_start_time) * 1000

    # print("Probable Partitions:", I)
    # print("Distances:", D)

    most_prob = I[0][0]

    Gt = to_networkx(partition_list[most_prob])
    Gpos = to_networkx(Gpos)
    query_nodes = Gq.num_nodes
    Gq = to_networkx(Gq)
    d = to_networkx(DATASET)

    print("\n\nRunning Model + FAISS...")
    print("=" * 80)

    print("Number of query nodes:", query_nodes)

    print("Most Probable Partition:", most_prob)

    total_time = model_time + faiss_time

    print("Model Time:", model_time)
    print("FAISS Time:", faiss_time)
    print("Total Time:", total_time)

    print(f"\n\nRunning VF3 on Partition {most_prob}...")
    print("=" * 80)

    vf3_start_time = time.time()
    vf3_is_subgraph = vf3py.has_subgraph(Gq, Gt)
    print(f"VF3 Result: {vf3_is_subgraph}")
    vf3_isomorphisms = vf3py.get_subgraph_isomorphisms(Gq, Gt)
    vf3_end_time = time.time()
    vf3_time = (vf3_end_time - vf3_start_time) * 1000
    print(f"VF3 Time: {vf3_time}")
    print(f"VF3 Mapping: {vf3_isomorphisms[0]}")

    print(f"\n\nRunning VF2 on Partition {most_prob}...")
    print("=" * 80)

    vf2_start_time = time.time()
    matcher = nx.algorithms.isomorphism.GraphMatcher(Gt, Gq)
    vf2_is_subgraph = matcher.subgraph_is_isomorphic()
    print(f"VF2 Result: {vf2_is_subgraph}")
    vf2_isomorphisms = matcher.subgraph_isomorphisms_iter()
    vf2_end_time = time.time()
    vf2_time = (vf2_end_time - vf2_start_time) * 1000
    print(f"VF2 Time: {vf2_time}")
    print(f"VF2 Mapping: {vf2_isomorphisms[0]}")


if __name__ == "__main__":
    main()
