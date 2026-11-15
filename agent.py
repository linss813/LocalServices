#!/usr/bin/env python3
"""Standalone agent entry point: no web page assets needed."""
import sys
from manager import main
if __name__=='__main__':
    sys.argv.extend(['--mode','agent'])
    main()
