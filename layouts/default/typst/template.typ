// DocWizard layouts: PDF via Typst. Every setting comes from the layout's design (the dw
// dictionary in design.typ, generated from layout.json), so layouts and the layout editor
// don't need template code of their own. Based on pandoc's default Typst template (GPL-2.0-or-later).

#show terms.item: it => block(breakable: false)[
  #text(weight: "bold")[#it.term]
  #block(inset: (left: 1.5em, top: -0.4em))[#it.description]
]

#set table(
  inset: 6pt,
  stroke: none
)

$if(highlighting-definitions)$
// syntax highlighting functions from skylighting:
$highlighting-definitions$

$endif$
#let content-to-string(content) = {
  if content.has("text") {
    content.text
  } else if content.has("children") {
    content.children.map(content-to-string).join("")
  } else if content.has("body") {
    content-to-string(content.body)
  } else if content == [ ] {
    " "
  }
}
#let dw-section-numbering(value) = (
  "1.1": "1", "1.a": "1.a", "I.1": "I.1", "": "",
).at(value, default: value)

$design()$

// -- helpers ---------------------------------------------------------------
#let dw-fonts(name, fallback) = if name == "" { (fallback,) } else { (name, fallback) }
#let dw-weight(w) = ("bold": "bold", "semibold": "semibold", "regular": "regular").at(w, default: "regular")
#let dw-case(case, body) = if case == "upper" { upper(body) } else if case == "smallcaps" { smallcaps(body) } else { body }
#let dw-align(a) = if a == "center" { center } else { left }
#let dw-filled(x) = x != none and x != [] and x != "" and x != ()
#let dw-leading(factor) = factor * 0.65em
// Drops the spaces pandoc leaves around a caption's text, so "Figure 1. Text" has one space.
#let dw-trim(body) = {
  if body.func() == [].func() {
    let kids = body.children
    while kids.len() > 0 and kids.first().func() == [ ].func() { kids = kids.slice(1) }
    while kids.len() > 0 and kids.last().func() == [ ].func() { kids = kids.slice(0, -1) }
    kids.join()
  } else { body }
}
#let dw-numbering(style) = {
  if style == "" { none }
  else if style == "1" { "1.1" }
  else if style == "1." { "1.1." }
  else if style == "1.a" { "1.a.i" }
  else if style == "I.1" { "I.1" }
  else if style == "I.A.1" {
    (..nums) => {
      let n = nums.pos()
      let pattern = ("I.", "A.", "1.", "a.").at(calc.min(n.len(), 4) - 1)
      numbering(pattern, n.last())
    }
  } else if style == "trad" {
    (..nums) => {
      let n = nums.pos()
      let pattern = ("I.", "1.", "a)", "α)").at(calc.min(n.len(), 4) - 1)
      numbering(pattern, n.last())
    }
  } else if style == "ieee" {
    // IEEE Editorial Style Manual: I., A., 1), and 1a) for the fourth level.
    (..nums) => {
      let n = nums.pos()
      if n.len() == 1 { numbering("I.", n.at(0)) }
      else if n.len() == 2 { numbering("A.", n.at(1)) }
      else if n.len() == 3 { numbering("1)", n.at(2)) }
      else { numbering("1", n.at(2)) + numbering("a)", n.last()) }
    }
  } else { style }
}
#let dw-page-numbering(fmt) = if fmt == "none" { none } else { fmt }
#let dw-styled(style, body) = {
  if style == "bold" { text(weight: "bold", body) }
  else if style == "italic" { emph(body) }
  else if style == "small" { text(size: 0.85em, body) }
  else if style == "large" { text(size: 1.2em, body) }
  else { body }
}
#let dw-level(level) = "heading-" + str(calc.min(level, 4)) + "-"

// Scene breaks: *** or --- in the text.
#let horizontalrule = {
  let kind = dw.at("text-scene-break")
  if kind == "rule" { align(center, line(length: 50%, stroke: 0.5pt)) }
  else if kind == "blank" { v(1em) }
  else { block(width: 100%, above: 0pt, below: 0pt, par(first-line-indent: 0pt, justify: false, align(center, kind))) }
}

// A heading that runs into its paragraph (APA levels 4 and 5, IEEE's third level).
// filters/runin.lua puts it at the start of the paragraph.
#let dw-runin(level, body) = {
  counter(heading).step(level: level)
  let p = dw-level(level)
  let punct = if dw.at(p + "run-in") == "colon" { ":" } else if dw.at(p + "run-in") == "period" { "." } else { "" }
  let fmt = dw-numbering(dw.at("heading-numbering"))
  h(dw.at(p + "indent"))
  text(
    weight: dw-weight(dw.at(p + "weight")),
    style: if dw.at(p + "style") == "italic" { "italic" } else { "normal" },
    {
      if fmt != none { context counter(heading).display(fmt); h(0.4em) }
      dw-case(dw.at(p + "case"), body)
      punct
    },
  )
  h(0.5em)
}

// -- scripts: filters/screenplay.lua and filters/avscript.lua --------------------
#let dw-scene(body, right: none, number: none) = {
  set par(first-line-indent: 0pt, justify: false)
  let line = dw-leading(dw.at("text-leading")) + 0.65em
  block(above: 2 * line, below: line, sticky: true, width: 100%,
    text(weight: dw-weight(dw.at("script-scene-weight")), {
      if number != none { place(left, dx: -0.75in, number) }
      if right != none { grid(columns: (1fr, auto), column-gutter: 1em, body, right) } else { body }
      if number != none { place(right, dx: 0.6in, dy: 0pt, number) }
    }))
}
#let dw-action(body) = block(width: 100%, par(first-line-indent: 0pt, justify: false, body))
#let dw-character(body) = block(above: 1.3em, below: 0pt, sticky: true, width: 100%,
  pad(left: dw.at("script-character-indent"), par(first-line-indent: 0pt, justify: false, upper(body))))
#let dw-parenthetical(body) = block(above: 0pt, below: 0pt, sticky: true, width: 100%,
  pad(left: dw.at("script-parenthetical-indent"),
    block(width: dw.at("script-parenthetical-width"), par(first-line-indent: 0pt, justify: false, hanging-indent: 0.6em, body))))
#let dw-dialogue(body) = block(above: 0pt, width: 100%, breakable: false,
  pad(left: dw.at("script-dialogue-indent"),
    block(width: dw.at("script-dialogue-width"), par(first-line-indent: 0pt, justify: false, body))))
#let dw-transition(body) = block(width: 100%, sticky: false,
  align(right, par(first-line-indent: 0pt, justify: false, upper(body))))
#let dw-centered(body) = block(width: 100%, align(center, par(first-line-indent: 0pt, justify: false, body)))
#let dw-av(rows) = {
  let share = dw.at("script-video-width")
  let numbered = dw.at("script-row-numbers")
  let cols = if numbered { (2em, share * 1fr, (1 - share) * 1fr) } else { (share * 1fr, (1 - share) * 1fr) }
  let head = (if numbered { ([],) } else { () }) + (
    text(weight: "bold", dw.at("script-video-label")), text(weight: "bold", dw.at("script-audio-label")))
  set par(first-line-indent: 0pt, justify: false)
  table(
    columns: cols,
    stroke: (x, y) => (top: 0.5pt, bottom: 0.5pt, left: if x > 0 { 0.5pt } else { 0pt }),
    inset: 6pt,
    table.header(..head),
    ..rows.enumerate().map(((i, row)) => (if numbered { ([#(i + 1)],) } else { () }) + row).flatten(),
  )
}

#let conf(
  title: none,
  subtitle: none,
  authors: (),
  keywords: (),
  date: none,
  abstract-title: none,
  abstract: none,
  thanks: none,
  lang: none,
  region: none,
  margin: none,
  mathfont: none,
  meta: (:),
  terms: (:),
  declaration: none,
  toc: false,
  toc-depth: none,
  toc-title: none,
  lof-title: none,
  lot-title: none,
  doc,
) = {
  set document(title: title, keywords: keywords)
  set document(
    author: authors.map(author => content-to-string(author.name)).join(", ", last: " & "),
  ) if authors != none and authors != ()

  let term(key, fallback) = terms.at(key, default: fallback)
  let surname = if authors != () {
    let name = content-to-string(authors.first().name)
    if name != none { name.trim().split(" ").last() } else { "" }
  } else { "" }
  let short-title = if dw-filled(meta.at("short-title", default: none)) { meta.at("short-title") } else { title }

  // Page
  let numfmt = dw-page-numbering(dw.at("page-number-format"))
  let front = dw.at("page-front-numbering")
  let back = dw.at("page-back-numbering") and front != ""
  let pos = dw.at("page-number-position")
  // The page's own number: roman in the front matter (and the back matter, when it continues it).
  let page-fmt(loc) = {
    let body = query(<dw-body>)
    let tail = query(<dw-back>)
    if front != "" and body.len() > 0 and loc.page() < body.first().location().page() { front }
    else if back and tail.len() > 0 and loc.page() >= tail.first().location().page() { front }
    else { numfmt }
  }
  let pagenum = context {
    let fmt = page-fmt(here())
    let start = query(<dw-text>)
    let first-page = if start.len() > 0 { start.first().location().page() } else { 1 }
    let hide = not dw.at("page-number-first") and here().page() == first-page
    if fmt != none and not hide {
      let label = dw.at("page-number-label")
      let num = counter(page).display(fmt, both: fmt.contains("/"))
      if label == "surname" [#surname #num]
      else if label == "surname-title" [#surname / #short-title / #num]
      else { num }
    }
  }
  let header-kind = dw.at("page-header")
  let header-text = if header-kind == "title" { title }
    else if header-kind == "author" and authors != () { authors.map(a => a.name).join(", ") }
    else if header-kind == "author-title" {
      let names = if authors != () { authors.map(a => a.name).join(", ") } else { [] }
      [#names #h(1fr) #title]
    } else { none }
  let num-align = if pos.ends-with("right") { right } else if pos.ends-with("left") { left } else { center }
  let header = context {
    let first = here().page() == 1
    let parts = ()
    if header-text != none and not first {
      parts.push(text(size: 0.85em, header-text))
    }
    if pos.starts-with("top") and numfmt != none {
      parts.push(align(num-align, pagenum))
    }
    if parts.len() > 0 {
      if header-text != none and not first and pos.starts-with("top") {
        grid(columns: (1fr, auto), align: (left, right), text(size: 0.85em, header-text), pagenum)
      } else {
        parts.join()
      }
      if dw.at("page-header-rule") and header-text != none and not first {
        v(-0.4em)
        line(length: 100%, stroke: 0.4pt)
      }
    }
  }
  let footer = if pos.starts-with("bottom") and numfmt != none { align(num-align, pagenum) } else { none }
  let page-margin = if margin != none { margin } else {
    (top: dw.at("page-margin-top"), bottom: dw.at("page-margin-bottom"),
     left: dw.at("page-margin-left"), right: dw.at("page-margin-right"))
  }
  let cols = int(dw.at("page-columns"))
  // A fixed distance puts the top of a header number, or the baseline of a footer
  // number, that far from the page edge (Turabian: half an inch).
  let num-distance = dw.at("page-number-distance")
  let cap = dw.at("text-size") * 0.7pt
  let fixed = num-distance != none and margin == none
  set page(
    paper: dw.at("page-paper"),
    margin: page-margin,
    columns: cols,
    header: header,
    footer: footer,
    header-ascent: if fixed and pos.starts-with("top") { dw.at("page-margin-top") - num-distance - cap } else { 30% },
    footer-descent: if fixed and pos.starts-with("bottom") { dw.at("page-margin-bottom") - num-distance - cap } else { 30% },
  )
  set columns(gutter: dw.at("page-column-gap"))

  // Text
  let leading = dw-leading(dw.at("text-leading"))
  set text(
    font: dw-fonts(dw.at("text-font"), "Libertinus Serif"),
    size: dw.at("text-size") * 1pt,
    hyphenate: dw.at("text-hyphenate"),
  )
  set text(lang: lang) if lang != none
  set text(region: region) if region != none
  set par(
    justify: dw.at("text-justify"),
    leading: leading,
    spacing: leading + dw.at("text-paragraph-spacing"),
    first-line-indent: (amount: dw.at("text-indent"), all: dw.at("text-indent-first")),
  )
  show math.equation: set text(font: mathfont) if mathfont != none
  show raw: set text(font: dw-fonts(dw.at("text-code-font"), "DejaVu Sans Mono"))
  show link: set text(fill: rgb(dw.at("text-link-color"))) if dw.at("text-link-color") != ""

  // Headings
  set heading(numbering: dw-numbering(dw.at("heading-numbering")))
  let by-chapter = dw.at("caption-by-chapter")
  show heading: it => {
    let p = dw-level(it.level)
    if it.level == 1 and dw.at("heading-chapter-break") { pagebreak(weak: true) }
    if it.level == 1 and by-chapter {
      counter(figure.where(kind: image)).update(0)
      counter(figure.where(kind: table)).update(0)
    }
    set text(
      // Relative to the body text, not to Typst's own heading sizes (1.4em, 1.2em) that
      // are already applied to the heading.
      size: dw.at(p + "size") * dw.at("text-size") * 1pt,
      weight: dw-weight(dw.at(p + "weight")),
      style: if dw.at(p + "style") == "italic" { "italic" } else { "normal" },
      hyphenate: false,
    )
    set text(font: dw-fonts(dw.at("heading-font"), "Libertinus Serif")) if dw.at("heading-font") != ""
    set par(first-line-indent: 0pt, justify: false)
    let number = if it.numbering != none { counter(heading).display(it.numbering) + h(0.6em) }
    let indent = if it.level >= 3 { dw.at(p + "indent") } else { 0pt }
    block(above: dw.at(p + "above"), below: dw.at(p + "below"), sticky: true, width: 100%,
      pad(left: indent, align(dw-align(dw.at(p + "align")), dw-case(dw.at(p + "case"), [#number#it.body]))))
  }

  // Block quotations
  show quote.where(block: true): it => {
    set text(size: dw.at("quote-size") * 1em)
    set text(style: "italic") if dw.at("quote-italic")
    set par(leading: dw-leading(dw.at("quote-leading")), spacing: dw-leading(dw.at("quote-leading")) + 0.4em) if dw.at("quote-leading") > 0
    let space = if dw.at("quote-spacing") != none { dw.at("quote-spacing") } else { auto }
    block(inset: (left: dw.at("quote-indent-left"), right: dw.at("quote-indent-right")), width: 100%,
      above: space, below: space, it.body)
  }

  // Footnotes
  set footnote.entry(
    separator: if dw.at("footnote-rule") { line(length: 30%, stroke: 0.5pt) } else { none },
    indent: dw.at("footnote-indent"),
    gap: dw.at("footnote-spacing"),
  )
  show footnote.entry: set text(size: dw.at("footnote-size") * 1em)
  show footnote.entry: set par(leading: dw-leading(dw.at("footnote-leading")), first-line-indent: 0pt) if dw.at("footnote-leading") > 0
  // Turabian sets note numbers as "1. " in the notes; Typst's own entry has a bare superscript.
  show footnote.entry: it => {
    let style = dw.at("footnote-number")
    if style == "super" { return it }
    let note = it.note
    let num = context numbering(note.numbering, ..counter(footnote).at(note.location()))
    let mark = if style == "period" [#num.~] else [#super(num)~]
    par(first-line-indent: 0pt)[#h(dw.at("footnote-indent"))#mark#note.body]
  }

  // Bibliography (pandoc's citeproc output: a block labelled refs, entries labelled ref-…)
  show <refs>: set par(
    hanging-indent: dw.at("bib-hanging"),
    first-line-indent: 0pt,
    justify: false,
  )
  let bib-leading = if dw.at("bib-leading") > 0 { dw-leading(dw.at("bib-leading")) } else { leading }
  show <refs>: set par(leading: bib-leading)
  show <refs>: set text(size: dw.at("bib-size") * 1em)
  show <refs>: set block(spacing: bib-leading + dw.at("bib-spacing"))
  let back-mark = if back {
    [#metadata(none) <dw-back>]
    context counter(page).update(counter(page).at(query(<dw-front-end>).first().location()).first() + 1)
  }
  show <bibliography>: it => {
    if dw.at("bib-page-break") { pagebreak(weak: true) }
    back-mark
    let style = dw.at("bib-heading-style")
    if style == "heading" { it } else {
      let p = dw-level(1)
      block(above: dw.at(p + "above"), below: dw.at(p + "below"), sticky: true, width: 100%,
        align(center, text(size: dw.at(p + "size") * dw.at("text-size") * 1pt, hyphenate: false,
          weight: if style == "center-plain" { "regular" } else { dw-weight(dw.at(p + "weight")) }, it.body)))
    }
  }
  show <refs>: it => {
    if dw.at("bib-page-break") {
      context if query(<bibliography>).len() == 0 { pagebreak(weak: true) }
    }
    context if query(<bibliography>).len() == 0 { back-mark }
    it
  }

  // Figures and tables
  show figure.where(kind: image): set figure(supplement: dw.at("caption-figure")) if dw.at("caption-figure") != ""
  show figure.where(kind: table): set figure(supplement: dw.at("caption-table")) if dw.at("caption-table") != ""
  let chapter-numbering(style) = (..n) => {
    let chapter = counter(heading).get().first()
    numbering("1", chapter) + "." + numbering(style, ..n)
  }
  show figure.where(kind: table): set figure(numbering: if by-chapter { chapter-numbering(dw.at("caption-table-numbering")) } else { dw.at("caption-table-numbering") })
  show figure.where(kind: image): set figure(numbering: chapter-numbering("1")) if by-chapter
  show figure.where(kind: table): set figure.caption(position: if dw.at("caption-table-position") == "top" { top } else { bottom })
  show figure.where(kind: image): set figure.caption(position: if dw.at("caption-figure-position") == "top" { top } else { bottom })
  set figure.caption(separator: dw.at("caption-separator"))
  show figure.caption: set text(size: dw.at("caption-size") * 1em)
  show figure.caption: it => {
    let body = dw-case(dw.at("caption-title-case"), dw-trim(it.body))
    if dw.at("caption-title-style") == "italic" { body = emph(body) }
    let caption = if it.numbering == none { body } else {
      let label = text(weight: dw-weight(dw.at("caption-label-weight")),
        [#it.supplement~#context it.counter.display(it.numbering)])
      if dw.at("caption-break") [#label#linebreak()#body] else [#label#it.separator#body]
    }
    if dw.at("caption-align") == "left" { block(width: 100%, align(left, caption)) } else { caption }
  }
  show figure.caption: set par(leading: dw-leading(dw.at("caption-leading"))) if dw.at("caption-leading") > 0

  // Title block and title page
  let field(key) = {
    if key == "author" {
      if authors != () { authors.map(a => a.name) } else { () }
    } else if key == "byline" {
      if authors != () {
        let names = authors.map(a => a.name)
        let joined = if names.len() > 1 { names.slice(0, -1).join(", ") + [ #term("and", "and") ] + names.last() } else { names.first() }
        ([#term("by", "by") #joined],)
      } else { () }
    } else if key == "affiliation" {
      authors.map(a => a.affiliation).filter(dw-filled).dedup()
    } else if key == "email" {
      let mails = authors.map(a => a.email).filter(dw-filled)
      if mails.len() == 0 and dw-filled(meta.at("email", default: none)) { (meta.email,) } else { mails }
    } else if key == "keywords" {
      if keywords != () { (keywords.join(", "),) } else { () }
    } else if key == "date" {
      if dw-filled(date) { (date,) } else { () }
    } else if key == "place-date" {
      if dw-filled(date) {
        let place = meta.at("place", default: none)
        (if dw-filled(place) [#place, #date] else { date },)
      } else { () }
    } else {
      let value = meta.at(key, default: none)
      if dw-filled(value) { (value,) } else { () }
    }
  }
  let abstract-block = {
    let heading = if dw.at("cover-abstract-title") != "" { dw.at("cover-abstract-title") }
      else if abstract-title != none { abstract-title } else { none }
    let sep = dw.at("cover-abstract-separator")
    block(above: 1.5em, inset: (x: if dw.at("cover-page") or sep != "space" { 0em } else { 2em }), width: 100%)[
      #set align(left)
      #set par(justify: dw.at("text-justify"), first-line-indent: 0pt)
      #if heading == none { abstract }
      else if sep == "em-dash" [#text(weight: "bold", style: "italic")[#heading]—#text(weight: "bold", abstract)]
      else if sep == "break" [#align(center, text(weight: "bold", heading))#abstract]
      else [#text(weight: "semibold")[#heading] #h(0.8em) #abstract]
    ]
  }
  let render-fields(items, spacing: dw.at("cover-field-spacing")) = {
    for item in items {
      if "space" in item {
        v(item.space)
      } else if "text" in item {
        block(above: spacing, below: spacing, dw-styled(item.at("style", default: "normal"), item.text))
      } else if item.key == "abstract" {
        if abstract != none and dw.at("cover-abstract") and not dw.at("cover-abstract-page") { abstract-block }
      } else {
        let values = field(item.key)
        if values.len() > 0 {
          let label = item.at("label", default: none)
          let body = values.join(linebreak())
          let labelled = if label == none { body } else if label.ends-with("—") [#label#body] else [#label #body]
          block(above: spacing, below: spacing, dw-styled(item.at("style", default: "normal"), labelled))
        }
      }
    }
  }
  let cover-page = dw.at("cover-page")
  let has-abstract-field = dw.at("cover-fields").any(i => i.at("key", default: none) == "abstract")
  let title-text(size-factor: 1) = text(
    weight: dw-weight(dw.at("cover-title-weight")),
    size: dw.at("cover-title-size") * 1em,
    hyphenate: false,
    font: if dw.at("heading-font") != "" { dw-fonts(dw.at("heading-font"), "Libertinus Serif") } else { dw-fonts(dw.at("text-font"), "Libertinus Serif") },
  )[#dw-case(dw.at("cover-title-case"), title)#if subtitle != none and dw.at("cover-title-colon") [:]#if thanks != none {
      footnote(thanks, numbering: "*")
      counter(footnote).update(n => n - 1)
    }]
  let title-align = if dw.at("cover-title-align") == "" { dw-align(dw.at("cover-align")) } else { dw-align(dw.at("cover-title-align")) }
  let title-block = {
    set par(first-line-indent: 0pt, justify: false, spacing: leading + 0.3em)
    set align(dw-align(dw.at("cover-align")))
    if dw.at("cover-fields-first") {
      render-fields(dw.at("cover-fields"))
    }
    if title != none {
      block(width: 100%, above: if dw.at("cover-fields-first") { leading + 0.65em } else { auto },
        below: if subtitle != none { calc.max(0.6em, leading) } else { 0.6em }, align(title-align, title-text()))
    }
    if subtitle != none {
      block(width: 100%, below: 1.2em, align(title-align, text(size: dw.at("cover-subtitle-size") * 1em,
        weight: dw-weight(dw.at("cover-subtitle-weight")), hyphenate: false, subtitle)))
    } else if not dw.at("cover-fields-first") {
      v(0.6em)
    }
    if not dw.at("cover-fields-first") {
      render-fields(dw.at("cover-fields"))
    }
    if dw.at("cover-abstract") and abstract != none and not has-abstract-field and not dw.at("cover-abstract-page") {
      abstract-block
    }
  }
  // Manuscripts: contact block top left, word count top right, single-spaced.
  let corners = {
    let l = dw.at("cover-corner-left")
    let r = dw.at("cover-corner-right")
    if l.len() > 0 or r.len() > 0 {
      set par(first-line-indent: 0pt, justify: false, leading: 0.65em, spacing: 0.65em)
      grid(columns: (1fr, auto), align: (left, right),
        render-fields(l, spacing: 0pt), render-fields(r, spacing: 0pt))
    }
  }

  if cover-page {
    let pos = dw.at("cover-position")
    page(header: none, footer: none, columns: 1, {
      corners
      if pos == "third" { v(1fr) } else if pos == "center" { v(1fr) }
      title-block
      if pos == "third" { v(2fr) } else { v(1fr) }
    })
    if not dw.at("cover-counted") { counter(page).update(1) }
  } else {
    corners
    if title != none or authors != () {
      if dw.at("cover-offset") != 0pt { v(dw.at("cover-offset")) }
      if cols > 1 {
        place(top + center, scope: "parent", float: true, block(width: 100%, below: 1.5em, title-block))
      } else {
        block(width: 100%, below: 2em, title-block)
      }
    }
  }
  if not cover-page [#metadata(none) <dw-text>]

  if dw.at("cover-abstract-page") and dw.at("cover-abstract") and abstract != none {
    let heading = if dw.at("cover-abstract-title") != "" { dw.at("cover-abstract-title") }
      else if abstract-title != none { abstract-title } else { none }
    page(columns: 1, {
      set par(first-line-indent: 0pt)
      if heading != none { align(center, text(weight: "bold", heading)) }
      abstract
      if keywords != () {
        par(first-line-indent: dw.at("text-indent"))[#emph[#term("keywords", "Keywords"):] #keywords.join(", ")]
      }
    })
  }

  if dw.at("declaration-enabled") {
    page(columns: 1, {
      set par(first-line-indent: 0pt)
      block(below: 1.5em, text(weight: "bold", size: 1.2em, dw.at("declaration-title")))
      if declaration != none { declaration } else {
        for para in dw.at("declaration-text").split("\n\n") { par(para.trim()) }
      }
      v(4em)
      line(length: 60%, stroke: 0.5pt)
      text(size: 0.9em, dw.at("declaration-signature"))
    })
  }

  if toc or dw.at("toc-figures") or dw.at("toc-tables") {
    let title = if dw.at("toc-title") != "" { dw.at("toc-title") } else if toc-title != none { toc-title } else { auto }
    let depth = if toc-depth != none { toc-depth } else { dw.at("toc-depth") }
    let lists = {
      if toc { outline(title: title, depth: depth) }
      if dw.at("toc-figures") and query(figure.where(kind: image)).len() > 0 {
        outline(title: if lof-title != none { lof-title } else { auto }, target: figure.where(kind: image))
      }
      if dw.at("toc-tables") and query(figure.where(kind: table)).len() > 0 {
        outline(title: if lot-title != none { lot-title } else { auto }, target: figure.where(kind: table))
      }
    }
    if dw.at("toc-page") {
      page(columns: 1, context lists)
    } else {
      block(below: 2em, context lists)
    }
  }

  if front != "" {
    [#metadata(none) <dw-front-end>]
    pagebreak(weak: true)
    counter(page).update(1)
  }
  [#metadata(none) <dw-body>]
  if cover-page [#metadata(none) <dw-text>]

  if dw.at("cover-repeat-title") and cover-page and title != none {
    block(width: 100%, below: leading + 0.65em, align(center, text(weight: "bold", hyphenate: false,
      [#title#if subtitle != none [: #subtitle]])))
  }

  doc

  if dw.at("script-end-mark") != "" {
    block(above: 2em, width: 100%, align(center, dw.at("script-end-mark")))
  }
}

$if(smart)$
$else$
#set smartquote(enabled: false)

$endif$
$for(header-includes)$
$header-includes$

$endfor$
#show: doc => conf(
$if(title)$
  title: [$title$],
$endif$
$if(subtitle)$
  subtitle: [$subtitle$],
$endif$
$if(author)$
  authors: (
$for(author)$
$if(author.name)$
    ( name: [$author.name$],
      affiliation: [$author.affiliation$],
      email: [$author.email$] ),
$else$
    ( name: [$author$],
      affiliation: "",
      email: "" ),
$endif$
$endfor$
    ),
$endif$
$if(keywords)$
  keywords: ($for(keywords)$"$keywords$",$endfor$),
$endif$
$if(date)$
  date: [$date$],
$endif$
$if(lang)$
  lang: "$lang$",
$endif$
$if(region)$
  region: "$region$",
$endif$
$if(abstract-title)$
  abstract-title: [$abstract-title$],
$endif$
$if(abstract)$
  abstract: [$abstract$],
$endif$
$if(thanks)$
  thanks: [$thanks$],
$endif$
$if(margin)$
  margin: ($for(margin/pairs)$$margin.key$: $margin.value$,$endfor$),
$endif$
$if(mathfont)$
  mathfont: ($for(mathfont)$"$mathfont$",$endfor$),
$endif$
$if(declaration)$
  declaration: [$declaration$],
$endif$
$if(toc-title)$
  toc-title: [$toc-title$],
$endif$
$if(lof-title)$
  lof-title: [$lof-title$],
$endif$
$if(lot-title)$
  lot-title: [$lot-title$],
$endif$
  terms: (
$if(dw-terms)$
    "by": [$dw-terms.by$],
    "and": [$dw-terms.and$],
    "keywords": [$dw-terms.keywords$],
$endif$
  ),
  meta: (
    dw-none: none,
$if(paper-type)$    paper-type: [$paper-type$],
$endif$
$if(course)$    course: [$course$],
$endif$
$if(instructor)$    instructor: [$instructor$],
$endif$
$if(supervisor)$    supervisor: [$supervisor$],
$endif$
$if(semester)$    semester: [$semester$],
$endif$
$if(department)$    department: [$department$],
$endif$
$if(faculty)$    faculty: [$faculty$],
$endif$
$if(institution)$    institution: [$institution$],
$endif$
$if(student-id)$    student-id: [$student-id$],
$endif$
$if(program)$    program: [$program$],
$endif$
$if(place)$    place: [$place$],
$endif$
$if(email)$    email: [$email$],
$endif$
$if(address)$    address: [$address$],
$endif$
$if(phone)$    phone: [$phone$],
$endif$
$if(word-count)$    word-count: [$word-count$],
$endif$
$if(genre)$    genre: [$genre$],
$endif$
$if(draft)$    draft: [$draft$],
$endif$
$if(contact)$    contact: [$contact$],
$endif$
$if(submission)$    submission: [$submission$],
$endif$
$if(birthplace)$    birthplace: [$birthplace$],
$endif$
$if(short-title)$    short-title: [$short-title$],
$endif$
  ),
$if(toc)$
  toc: true,
$endif$
  doc,
)

$for(include-before)$
$include-before$

$endfor$
$body$

$for(include-after)$

$include-after$
$endfor$
