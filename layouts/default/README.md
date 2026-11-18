# Default layout

Clean single-column output for any kind of document, and the base of every other layout.

- The templates read every setting from the layout's design (`design` in `layout.json`): page, body text,
  headings, title block and title page, declaration page, table of contents, block quotations, footnotes,
  citations and bibliography, figures and tables. The settings and their defaults are in
  `server/docwizard/render/design.py`; the layout editor builds its form from the same list.
- HTML (preview and publishing): `html/template.html` and `html/style.css` for structure; `design.css`,
  `cover.html` and `options.html` are generated from the design when the layout is cached.
- PDF via Typst: `typst/template.typ`, a static template that loads the generated `design.typ`.
- DOCX and ODT use pandoc's default reference documents.

Options: paper size, font size, line spacing, section numbering, table of contents and citation style.
