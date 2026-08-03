from pathlib import Path
import runpy, sys
sys.exit(runpy.run_path(str(Path(__file__).resolve().parent.parent / "core" / "verify_chain.py"), run_name="__main__"))
