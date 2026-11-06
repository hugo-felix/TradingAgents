"""PyInstaller entry (absolute imports also work in a frozen worker)."""
from tradingagents.desktop.app import main

if __name__ == '__main__':
    main()
