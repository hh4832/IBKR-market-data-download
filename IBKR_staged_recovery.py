"""NQ + ES entry point; all scheduling, locking and pacing live in the shared runner."""
from NQ_staged_recovery import main

if __name__=='__main__':
    raise SystemExit(main())
