import sys
import logging
import argparse


def main(args: argparse.Namespace) -> int:
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    sys.exit(main(args))
