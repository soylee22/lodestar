"""Telegram failure notices for the Lodestar performance jobs."""
import argparse
import os

from notify import telegram


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['failure'])
    parser.parse_args()
    url = (f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'soylee22/lodestar')}"
           f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
    if not telegram('<b>Lodestar update needs attention</b>\n' + url):
        raise RuntimeError('Telegram failure notice was not accepted')


if __name__ == '__main__':
    main()
