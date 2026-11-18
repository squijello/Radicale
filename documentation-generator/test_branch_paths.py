"""Check that generated pages and links agree for slash-named branches."""

import json
import os
import subprocess
import unittest
import urllib.parse
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import patch

import run


class BranchPathsTest(unittest.TestCase):
    def test_slash_and_percent_branches_get_distinct_pages_and_links(self):
        branches = ["master", "feature/topic", "feature%2Ftopic", "gh-pages"]
        metadata_by_branch = {}
        git_push = []

        def git(*args):
            if args == ("rev-parse", "--abbrev-ref", "HEAD"):
                return ("gh-pages",)
            if args == ("rev-parse", "--remotes=origin"):
                return ("same-remote-tips",)
            if args == ("rev-parse", "--symbolic-full-name", "--remotes=origin"):
                return tuple("refs/remotes/origin/" + name for name in branches)
            if args[0] == "checkout":
                branch = args[-2]
                if branch == "gh-pages":
                    Path(run.DOCUMENTATION_SRC).unlink(missing_ok=True)
                else:
                    Path(run.DOCUMENTATION_SRC).write_text(branch, encoding="utf-8")
            elif args[0] == "diff":
                raise subprocess.CalledProcessError(1, args)
            elif args[0] == "push":
                git_push.append(args)
            elif args[0] not in {"fetch", "add", "commit"}:
                self.fail(f"Unexpected Git command: {args}")
            return ()

        def render(args, **kwargs):
            if args[0] == run.PANDOC_EXE:
                arg, = [arg for arg in args if arg.startswith("--metadata-file=")]
                metadata = json.loads(Path(arg.split("=", 1)[1]).read_text(encoding="utf-8"))
                metadata_by_branch[metadata["branch"]] = metadata
                return subprocess.CompletedProcess(args, 0, stdout=metadata["branch"].encode())
            self.assertEqual(args[0], run.VENV_EXECUTABLE)
            return subprocess.CompletedProcess(args, 0, stdout=kwargs["input"])

        with TemporaryDirectory() as directory:
            cwd = Path.cwd()
            try:
                os.chdir(directory)
                Path(run.REDIRECT_CONFIG_PATH).write_text('{"index": "feature/topic"}', encoding="utf-8")

                # The mocked renderer opens this file in the same process.
                # On Windows, a named temporary file with delete=True stays locked.
                def open_metadata(**kwargs):
                    return NamedTemporaryFile(dir=directory, delete=False, **kwargs)

                with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), \
                        patch.object(run, "run_git", side_effect=git), \
                        patch.object(run, "NamedTemporaryFile", side_effect=open_metadata), \
                        patch.object(run.subprocess, "run", side_effect=render):
                    run.main()

                self.assertEqual(set(metadata_by_branch), set(branches[:-1]))
                for metadata in metadata_by_branch.values():
                    links = {link["name"]: urllib.parse.unquote_plus(link["href"])
                             for link in metadata["branches"]}
                    self.assertEqual(set(links), set(branches[:-1]))
                    self.assertEqual(len(set(links.values())), len(links))
                    for branch, filename in links.items():
                        self.assertEqual(Path(filename).parent, Path("."))
                        self.assertEqual(Path(filename).read_text(encoding="utf-8"), branch)

                redirect = Path("index.html").read_text(encoding="utf-8")
                slash_link, = [link["href"] for link in metadata_by_branch["master"]["branches"]
                               if link["name"] == "feature/topic"]
                self.assertIn(slash_link, redirect)
                self.assertEqual(git_push, [("push", "origin", "HEAD:gh-pages")])
            finally:
                os.chdir(cwd)
