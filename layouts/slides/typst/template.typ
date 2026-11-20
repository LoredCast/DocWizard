// DocWizard slides: PDF via Typst. slides.lua turns the document into #dw-slide(...)[...] calls;
// this template draws them as 16:9 (or 4:3) pages, or, with the handout option, as A4 pages
// with several slides and their notes (or lines for notes). Fonts and the accent colour come
// from the layout's design (the dw dictionary), so the layout editor can change them.

#let horizontalrule = line(length: 100%, stroke: 0.5pt + luma(60%))

#show terms.item: it => block(breakable: false)[
  #text(weight: "bold")[#it.term]
  #block(inset: (left: 1.5em, top: -0.4em))[#it.description]
]

#set table(inset: 6pt, stroke: none)

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

$design()$

#let dw-fonts(name, fallback) = if name == "" { (fallback,) } else { (name, fallback) }
#let dw-handout = $if(handout)$true$else$false$endif$
#let dw-numbers = $if(slide-numbers)$true$else$false$endif$
#let dw-dark = "$theme$" == "dark"
#let dw-wide = "$aspect$" != "4:3"
#let dw-deck-title = [$if(title)$$title$$endif$]

#let dw-slide-w = 25.4cm
#let dw-slide-h = if dw-wide { 14.2875cm } else { 19.05cm }
#let dw-slide-margin = (x: 1.8cm, top: 1.3cm, bottom: 1.5cm)
#let dw-link = dw.at("text-link-color")
#let dw-accent = if dw-link != "" { rgb("#" + dw-link) } else { rgb("#1a4fa0") }
#let dw-accent = if dw-dark { dw-accent.lighten(45%) } else { dw-accent }
#let dw-bg = if dw-dark { rgb("#15181e") } else { white }
#let dw-fg = if dw-dark { rgb("#eceff4") } else { rgb("#1d2026") }
#let dw-muted = if dw-dark { rgb("#a3abb9") } else { rgb("#5b6270") }
#let dw-body-font = dw-fonts(dw.at("text-font"), "Noto Sans")
#let dw-head-font = if dw.at("heading-font") == "" { dw-body-font } else { dw-fonts(dw.at("heading-font"), "Noto Sans") }
#let dw-size = dw.at("text-size") * 1.8pt

#set text(font: dw-body-font, hyphenate: false)
$if(lang)$
#set text(lang: "$lang$")
$endif$
$if(region)$
#set text(region: "$region$")
$endif$
#set par(justify: false, leading: 0.55em, spacing: 0.75em)
#set list(spacing: 0.6em, marker: ([•], [–], [·]))
#set enum(spacing: 0.6em)
#show raw: set text(font: dw-fonts(dw.at("text-code-font"), "DejaVu Sans Mono"), size: 0.85em)
#show link: set text(fill: dw-accent)
#show heading: set text(font: dw-head-font, fill: dw-accent)
#show heading: set block(above: 0.8em, below: 0.5em)
#show figure.caption: set text(size: 0.75em, fill: dw-muted)
// Pictures fit on the slide: at most the width, and about half the slide's height.
#show figure.where(kind: image): it => {
  show image: img => box(width: 100%, height: dw-slide-h * 0.55, align(center + horizon, img))
  set image(fit: "contain", height: 100%)
  it
}

// One part of the title slide: subtitle, author or date.
#let dw-part(kind, body) = {
  if kind == "subtitle" { block(above: 0.6em, text(size: 1.25em, fill: dw-muted, body)) }
  else if kind == "author" { block(above: 1.6em, text(size: 0.95em, weight: "semibold", body)) }
  else if kind == "date" { block(above: 0.4em, text(size: 0.85em, fill: dw-muted, body)) }
  else { body }
}

// What a slide shows, laid out on a slide-sized area.
#let dw-slide-body(kind, title, body) = {
  set text(fill: dw-fg, size: dw-size)
  if kind == "title" {
    align(horizon + left, block(width: 100%)[
      #text(font: dw-head-font, size: 2.1em, weight: "bold", fill: dw-accent, title)
      #v(0.2em)
      #line(length: 3.5em, stroke: 3pt + dw-accent)
      #body
    ])
  } else if kind == "section" {
    align(horizon + left, block(width: 100%, inset: (left: 0.8em, y: 0.3em), stroke: (left: 5pt + dw-accent))[
      #text(font: dw-head-font, size: 1.8em, weight: "bold", title)
      #if body != [] { block(above: 0.8em, text(fill: dw-muted, body)) }
    ])
  } else {
    if title != none {
      block(below: 0.9em, text(font: dw-head-font, size: 1.35em, weight: "bold", fill: dw-accent, title))
    }
    body
  }
}

#let dw-number(n) = text(size: 9pt, fill: dw-muted, str(n))

#let dw-slide(n: 0, kind: "slide", title: none, notes: none, body) = {
  if not dw-handout {
    pagebreak(weak: true)
    [#metadata(n) <dw-slide-n>]
    dw-slide-body(kind, title, body)
  } else {
    let frame = block(width: dw-slide-w, height: dw-slide-h, fill: dw-bg, inset: dw-slide-margin, clip: true, {
      dw-slide-body(kind, title, body)
      if dw-numbers and kind != "title" { place(bottom + right, dy: 0.9cm, dw-number(n)) }
    })
    let s = 38%
    block(breakable: false, below: 1.2em, grid(
      columns: (dw-slide-w * s, 1fr),
      column-gutter: 1.2em,
      block(stroke: 0.6pt + luma(65%), scale(x: s, y: s, reflow: true, frame)),
      {
        set text(size: 9.5pt, fill: black)
        set par(leading: 0.5em, spacing: 0.6em)
        if notes != none {
          notes
        } else {
          for i in range(6) { block(above: 0pt, below: 0.75cm, line(length: 100%, stroke: 0.4pt + luma(75%))) }
        }
      },
    ))
  }
}

#set document(title: [$pagetitle$])
#set page(..if dw-handout {
  (paper: "a4", margin: (x: 1.8cm, top: 2cm, bottom: 2cm), fill: none,
   header: align(right, text(size: 8pt, fill: luma(40%), dw-deck-title)),
   footer: context align(center, text(size: 8pt, fill: luma(40%), counter(page).display())))
} else {
  (width: dw-slide-w, height: dw-slide-h, margin: dw-slide-margin, fill: dw-bg,
   footer: context {
     let n = query(selector(<dw-slide-n>).before(here()))
     if dw-numbers and n.len() > 0 and n.last().value > 1 {
       grid(columns: (1fr, auto), text(size: 9pt, fill: dw-muted, dw-deck-title), dw-number(n.last().value))
     }
   })
})
#set text(size: if dw-handout { 10pt } else { dw-size }, fill: if dw-handout { black } else { dw-fg })

$for(header-includes)$
$header-includes$

$endfor$
$for(include-before)$
$include-before$

$endfor$
$body$
$for(include-after)$

$include-after$
$endfor$
