"""Project-root-independent entry point for the RAG benchmark."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from semi_rag.benchmark import main

if __name__ == '__main__':
    main()
