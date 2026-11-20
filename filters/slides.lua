--[[
slides.lua: a document as a slide deck, for the slides layouts (§9.7).

Slides follow pandoc's slide-show rules, so a deck written here also works with pandoc's own
writers:
  - a heading at the slide level (option slide-level, else the highest level that is followed by
    content rather than another heading) starts a slide with that title;
  - a heading above the slide level starts a section slide;
  - a horizontal rule (---) starts a slide without a title;
  - ::: notes holds the speaker's notes for the slide it is in;
  - ::: columns with ::: column inside sets content side by side;
  - . . . (a pause) is dropped: decks here are static.
The title, subtitle, author and date make a title slide; cited works get a last slide.

Typst: each slide is #dw-slide(n: .., kind: "..", title: [..], notes: [..])[body], which the
layout's template defines (and turns into handout pages when the handout option is on).
HTML: each slide is <section class="slide" id="slide-N"> with a .slide-frame and, hidden on screen,
the .notes. Other formats get the document with the pauses removed; PowerPoint makes its own
slides from the headings (pandoc's --slide-level), with the notes as speaker notes.
]]

local stringify = pandoc.utils.stringify

local function is_pause(b)
  return b.t == "Para" and #b.content == 5 and stringify(b.content) == ". . ."
end

local function meta_text(v)
  if v == nil then return nil end
  local s = stringify(v)
  if s == "" then return nil end
  return s
end

-- The slide level: the option, else the highest heading level followed directly by content.
local function slide_level(blocks, meta)
  local set = tonumber(meta_text(meta["slide-level"]) or "")
  if set and set >= 1 and set <= 6 then return math.floor(set) end
  local level
  for i, b in ipairs(blocks) do
    if b.t == "Header" then
      local nxt = blocks[i + 1]
      if nxt and nxt.t ~= "Header" and (level == nil or b.level < level) then level = b.level end
    end
  end
  return level or 2
end

local function has_cites(doc)
  local found = false
  doc:walk({ Cite = function() found = true end })
  return found
end

local function has_refs_div(blocks)
  local found = false
  pandoc.Pandoc(blocks):walk({ Div = function(d) if d.identifier == "refs" then found = true end end })
  return found
end

-- Split the body into slides: { kind, title (inlines or nil), id, classes, blocks, notes }.
local function split(blocks, level)
  local slides, current = {}, nil
  local function open(kind, title, attr)
    current = { kind = kind, title = title, id = attr and attr.identifier or "",
                classes = attr and attr.classes or {}, blocks = {}, notes = {} }
    slides[#slides + 1] = current
  end
  for _, b in ipairs(blocks) do
    if b.t == "Header" and b.level < level then
      open("section", b.content, b.attr)
    elseif b.t == "Header" and b.level == level then
      open("slide", b.content, b.attr)
    elseif b.t == "HorizontalRule" then
      open("slide", nil, nil)
    elseif is_pause(b) then
      -- dropped
    elseif b.t == "Div" and b.classes:includes("notes") then
      if not current then open("slide", nil, nil) end
      for _, x in ipairs(b.content) do current.notes[#current.notes + 1] = x end
    else
      if not current then open("slide", nil, nil) end
      if b.t == "Header" then b.level = math.min(6, b.level - level + 2) end
      current.blocks[#current.blocks + 1] = b
    end
  end
  return slides
end

local function title_slide(meta)
  if not meta.title then return nil end
  local blocks = {}
  if meta.subtitle then
    blocks[#blocks + 1] = pandoc.Div(pandoc.Plain(pandoc.Inlines(meta.subtitle)), { class = "subtitle" })
  end
  local authors = meta.author
  if authors then
    local list = (pandoc.utils.type(authors) == "List") and authors or { authors }
    local names = {}
    for _, a in ipairs(list) do
      local name = (pandoc.utils.type(a) == "table" or pandoc.utils.type(a) == "Meta") and a.name or a
      if name then names[#names + 1] = pandoc.Inlines(name) end
    end
    if #names > 0 then
      local line = pandoc.Inlines({})
      for i, n in ipairs(names) do
        if i > 1 then line:insert(pandoc.Str(",")) line:insert(pandoc.Space()) end
        line:extend(n)
      end
      blocks[#blocks + 1] = pandoc.Div(pandoc.Plain(line), { class = "author" })
    end
  end
  if meta.date then
    blocks[#blocks + 1] = pandoc.Div(pandoc.Plain(pandoc.Inlines(meta.date)), { class = "date" })
  end
  return { kind = "title", title = pandoc.Inlines(meta.title), id = "", classes = {}, blocks = blocks, notes = {} }
end

-- -- Typst -----------------------------------------------------------------------
local function raw(s) return pandoc.RawBlock("typst", s) end

local function typst_columns(div)
  local cols = {}
  for _, c in ipairs(div.content) do
    if c.t == "Div" and c.classes:includes("column") then cols[#cols + 1] = c end
  end
  if #cols == 0 then return nil end
  local widths = {}
  for i, c in ipairs(cols) do
    local w = c.attributes.width
    local pct = w and w:match("^(%d+%.?%d*)%%$")
    widths[i] = pct and (pct .. "%") or "1fr"
  end
  local out = { raw("#grid(columns: (" .. table.concat(widths, ", ") .. ",), column-gutter: 1.5em,") }
  for _, c in ipairs(cols) do
    out[#out + 1] = raw("[")
    for _, x in ipairs(c.content) do out[#out + 1] = x end
    out[#out + 1] = raw("],")
  end
  out[#out + 1] = raw(")")
  return out
end

local function typst_slide(s, n)
  local out = {}
  local head = string.format('#dw-slide(n: %d, kind: "%s"', n, s.kind)
  if s.title then
    out[#out + 1] = raw(head .. ", title: [")
    out[#out + 1] = pandoc.Plain(s.title)
    head = "]"
  end
  if #s.notes > 0 then
    out[#out + 1] = raw(head .. ", notes: [")
    for _, x in ipairs(s.notes) do out[#out + 1] = x end
    head = "]"
  end
  out[#out + 1] = raw(head .. ")[")
  if s.id ~= "" then out[#out + 1] = raw("#metadata(none) <" .. s.id .. ">") end
  for _, b in ipairs(s.blocks) do
    local part = b.t == "Div" and s.kind == "title" and b.classes[1]
    if part == "subtitle" or part == "author" or part == "date" then
      out[#out + 1] = raw('#dw-part("' .. part .. '")[')
      for _, x in ipairs(b.content) do out[#out + 1] = x end
      out[#out + 1] = raw("]")
    elseif b.t == "Div" and b.classes:includes("columns") then
      local cols = typst_columns(b)
      if cols then
        for _, x in ipairs(cols) do out[#out + 1] = x end
      else
        out[#out + 1] = b
      end
    else
      out[#out + 1] = b
    end
  end
  out[#out + 1] = raw("]")
  return out
end

-- -- HTML -------------------------------------------------------------------------
local function html_slide(s, n)
  local inner = {}
  if s.id ~= "" then
    -- Links to the heading's id still reach the slide.
    inner[#inner + 1] = pandoc.Plain({ pandoc.Span({}, { id = s.id }) })
  end
  if s.title then
    -- Not a heading element: pandoc's section divs would take the slide apart around it.
    inner[#inner + 1] = pandoc.Div({ pandoc.Plain(s.title) },
      { class = "slide-title", role = "heading", ["aria-level"] = s.kind == "slide" and "2" or "1" })
  end
  local body = {}
  for _, b in ipairs(s.blocks) do body[#body + 1] = b end
  inner[#inner + 1] = pandoc.Div(body, { class = "slide-body" })
  local content = { pandoc.Div(inner, { class = "slide-frame" }) }
  if #s.notes > 0 then content[#content + 1] = pandoc.Div(s.notes, { class = "notes" }) end
  local classes = { "slide", s.kind == "slide" and "content-slide" or (s.kind .. "-slide") }
  for _, c in ipairs(s.classes) do classes[#classes + 1] = c end
  return pandoc.Div(content, pandoc.Attr("slide-" .. n, classes, { ["data-n"] = tostring(n) }))
end

function Pandoc(doc)
  local typst = FORMAT == "typst"
  local html = FORMAT:match("^html") ~= nil
  if not typst and not html then
    -- Other formats keep the headings and notes; only the pauses go.
    local blocks = {}
    for _, b in ipairs(doc.blocks) do
      if not is_pause(b) then blocks[#blocks + 1] = b end
    end
    doc.blocks = blocks
    return doc
  end
  local level = slide_level(doc.blocks, doc.meta)
  local slides = split(doc.blocks, level)
  local first = title_slide(doc.meta)
  if first then table.insert(slides, 1, first) end
  if has_cites(doc) and not has_refs_div(doc.blocks) then
    local title = doc.meta["reference-section-title"]
    title = title and pandoc.Inlines(title) or pandoc.Inlines({ pandoc.Str("References") })
    slides[#slides + 1] = { kind = "slide", title = title, id = "", classes = { "references" },
                            blocks = { pandoc.Div({}, { id = "refs" }) }, notes = {} }
    -- The references go on that slide, not under a heading of citeproc's own after the deck.
    doc.meta["reference-section-title"] = nil
    doc.meta["dw-bib-heading"] = nil
  end
  local blocks = {}
  for n, s in ipairs(slides) do
    local part = typst and typst_slide(s, n) or { html_slide(s, n) }
    for _, b in ipairs(part) do blocks[#blocks + 1] = b end
  end
  doc.blocks = blocks
  doc.meta["dw-slide-count"] = pandoc.MetaString(tostring(#slides))
  return doc
end
