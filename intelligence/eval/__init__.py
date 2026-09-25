"""
Regression harness for clustering and headlines (see docs/ARCHITECTURE.md).

The reference corpus holds publisher text and stays out of git; only the
gold labels (gold.json, keyed by entry URL) are committed. Run from the
intelligence directory:

    python eval/export_corpus.py --out eval/corpus.json
    python evaluate.py --corpus eval/corpus.json --gold eval/gold.json --replay 25
"""
