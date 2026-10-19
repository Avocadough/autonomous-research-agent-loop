"""Repo-local four-operation launcher; callable by Muse from any cwd."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.quant_research_loop.controller import main

if __name__=='__main__':main()
