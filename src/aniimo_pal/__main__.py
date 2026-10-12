from aniimo_pal.cli import main

if __name__ == "__main__":  # required for multiprocessing workers on Windows (spawn)
    raise SystemExit(main())
