# Bureau field manual

Canonical content is README.md and docs/*.md. Run `python3 scripts/render_docs.py` from the repository root after editing those files. The optional documentation build dependency is Pandoc; the installed runtime does not need it. Generated HTML retains the shared field-manual CSS and can be opened directly as local files.

`tests/test_docs.sh` checks source hashes and local file links without requiring Pandoc. Do not hand-edit generated pages. The planning analysis in CODEX-SUPPORT-PLAN.md remains a historical Markdown document, linked from the acceptance record when useful.

The CSS design assets remain in `assets/colors_and_type.css` and `assets/docs.css`. No server, external font request, client JavaScript or account is required to read the field manual.
