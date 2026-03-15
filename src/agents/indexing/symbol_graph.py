"""Symbol relationship graph with self-contained PageRank.

Builds a directed graph of symbol definitions and references across files.
Uses a custom PageRank implementation (no networkx dependency).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from agents.indexing.treesitter import Symbol

logger = logging.getLogger(__name__)


@dataclass
class RankedSymbol:
    """A symbol with its PageRank score."""
    symbol: Symbol
    rank: float
    references: int  # number of files that reference this symbol


class SymbolGraph:
    """Directed graph of symbol definitions and references.

    Nodes are file paths. Edges represent references: if file A references
    a symbol defined in file B, there's an edge A -> B.
    """

    def __init__(self):
        # file_path -> list of Symbol definitions in that file
        self.definitions: dict[str, list[Symbol]] = defaultdict(list)
        # symbol_name -> file_path where it's defined (first definition wins)
        self.symbol_to_file: dict[str, str] = {}
        # Adjacency list: source_file -> set of target_files (references)
        self.edges: dict[str, set[str]] = defaultdict(set)
        # Reference counts: (symbol_name, defining_file) -> count of referencing files
        self.reference_counts: dict[tuple[str, str], set[str]] = defaultdict(set)

    def add_definition(self, symbol: Symbol, file_path: str) -> None:
        """Register a symbol definition in a file."""
        self.definitions[file_path].append(symbol)
        # First definition wins for name resolution
        if symbol.name not in self.symbol_to_file:
            self.symbol_to_file[symbol.name] = file_path

    def add_reference(self, symbol_name: str, defining_file: str, referencing_file: str) -> None:
        """Register that referencing_file uses a symbol defined in defining_file."""
        if referencing_file != defining_file:
            self.edges[referencing_file].add(defining_file)
            self.reference_counts[(symbol_name, defining_file)].add(referencing_file)

    def resolve_references(self, file_path: str, source_text: str) -> None:
        """Scan source_text for references to known symbols and add edges.

        Simple approach: check if any known symbol name appears in the source.
        This has false positives for common names but works well enough for ranking.
        """
        for symbol_name, defining_file in self.symbol_to_file.items():
            if defining_file == file_path:
                continue
            # Skip very short names (likely false positives)
            if len(symbol_name) < 3:
                continue
            if symbol_name in source_text:
                self.add_reference(symbol_name, defining_file, file_path)

    def pagerank(
        self,
        iterations: int = 20,
        damping: float = 0.85,
    ) -> dict[str, float]:
        """Compute PageRank scores for files in the graph.

        Files that are referenced by many other files get higher scores.
        This identifies the most "important" files in the codebase.
        """
        # Collect all nodes
        all_nodes: set[str] = set()
        all_nodes.update(self.edges.keys())
        for targets in self.edges.values():
            all_nodes.update(targets)
        all_nodes.update(self.definitions.keys())

        if not all_nodes:
            return {}

        n = len(all_nodes)
        nodes = sorted(all_nodes)
        node_idx = {node: i for i, node in enumerate(nodes)}

        # Initialize ranks uniformly
        ranks = [1.0 / n] * n

        # Build reverse adjacency (who points to each node)
        incoming: dict[int, list[int]] = defaultdict(list)
        outgoing_count: dict[int, int] = defaultdict(int)

        for source, targets in self.edges.items():
            src_idx = node_idx.get(source)
            if src_idx is None:
                continue
            outgoing_count[src_idx] = len(targets)
            for target in targets:
                tgt_idx = node_idx.get(target)
                if tgt_idx is not None:
                    incoming[tgt_idx].append(src_idx)

        # Iterate
        for _ in range(iterations):
            new_ranks = [(1 - damping) / n] * n
            for i in range(n):
                for j in incoming.get(i, []):
                    out_count = outgoing_count.get(j, 1)
                    new_ranks[i] += damping * ranks[j] / out_count
            ranks = new_ranks

        return {nodes[i]: ranks[i] for i in range(n)}

    def get_ranked_symbols(self, top_n: int | None = None) -> list[RankedSymbol]:
        """Get symbols ranked by their file's PageRank score.

        Returns symbols sorted by rank (highest first).
        """
        file_ranks = self.pagerank()

        ranked = []
        for file_path, symbols in self.definitions.items():
            file_rank = file_ranks.get(file_path, 0.0)
            for symbol in symbols:
                ref_count = len(self.reference_counts.get(
                    (symbol.name, file_path), set()
                ))
                ranked.append(RankedSymbol(
                    symbol=symbol,
                    rank=file_rank,
                    references=ref_count,
                ))

        ranked.sort(key=lambda r: (-r.rank, -r.references, r.symbol.file_path))

        if top_n is not None:
            ranked = ranked[:top_n]

        return ranked

    @property
    def file_count(self) -> int:
        return len(self.definitions)

    @property
    def symbol_count(self) -> int:
        return sum(len(syms) for syms in self.definitions.values())
