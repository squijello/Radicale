# Documentation generator on this fork

The GitHub Actions workflow checks out `gh-pages`, reads `DOCUMENTATION.md`
from the fork's branches, and commits the generated HTML to `gh-pages`.
Branches under `trial/` are skipped. GitHub Pages hosting is configured
separately in the repository settings.

Branch names are encoded in the filenames before those names are escaped for
links. This keeps `feature/topic` and a branch named `feature%2Ftopic` distinct
and lets their pages share the assets in the root directory.

Run the branch filename and link regression from the root of this checkout.

```sh
python -m unittest discover -s documentation-generator -p test_branch_paths.py
```
