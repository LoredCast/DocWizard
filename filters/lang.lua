--[[
lang.lua: the document's languages (ARCHITECTURE.md §9.8). Runs first, before the
layout's filters and citeproc.

A document declares its main language with `lang:` and the other languages it
uses with `otherlangs:` in its front matter; passages in another language are
marked `[text]{lang=fr}` or `::: {lang=en-GB}`. This filter

- normalises the tags (de_at -> de-AT, "German" -> de), so citeproc, Typst
  and the HTML lang attribute all get the same language;
- adds every language a passage uses to otherlangs (LaTeX loads them);
- writes ISO dates (date: 2026-11-16) in the language's format;
- fills in the generated headings and labels in the document's language:
  abstract, contents, lists of figures and tables, the bibliography heading
  the layout asks for, and the words crossref.lua puts in references.

Metadata from the layout (written by the app, overridden by front matter):
dw-date-format (long, day-month-year, numeric, iso, as-written), dw-bib-heading
(a term such as bibliography or works-cited), dw-caption-figure/-table (fixed
labels such as "Fig.").
]]

local L = require "dwlocale"
local stringify = pandoc.utils.stringify

local function text_of(value)
  if value == nil then return nil end
  local s = stringify(value)
  if s == "" then return nil end
  return s
end

local function inlines(text)
  local out = pandoc.Inlines({})
  local first = true
  for word in text:gmatch("%S+") do
    if not first then out:insert(pandoc.Space()) end
    out:insert(pandoc.Str(word))
    first = false
  end
  return pandoc.MetaInlines(out)
end

local function list_of(value)
  if value == nil then return {} end
  if pandoc.utils.type(value) == "List" then
    local out = {}
    for _, v in ipairs(value) do out[#out + 1] = stringify(v) end
    return out
  end
  local out = {}
  for part in stringify(value):gmatch("[^,%s]+") do out[#out + 1] = part end
  return out
end

function Pandoc(doc)
  local meta = doc.meta
  local raw = text_of(meta.lang)
  local main = raw and (L.normalize(raw) or raw)
  if main then meta.lang = pandoc.MetaString(main) end
  local loc = L.resolve(main or "en-US")

  -- Other languages: declared ones and every lang attribute in the text.
  local others, seen = {}, {}
  if main then seen[main] = true end
  local function add(tag)
    tag = L.normalize(tag) or tag
    if tag and not seen[tag] then
      seen[tag] = true
      others[#others + 1] = tag
    end
    return tag
  end
  for _, tag in ipairs(list_of(meta.otherlangs)) do add(tag) end
  local function fix(el)
    local tag = el.attributes.lang
    if tag then
      el.attributes.lang = add(tag)
      return el
    end
  end
  doc.blocks = doc.blocks:walk({ Span = fix, Div = fix })
  if #others > 0 then
    local list = pandoc.List()
    for _, tag in ipairs(others) do list:insert(pandoc.MetaString(tag)) end
    meta.otherlangs = list
  else
    meta.otherlangs = nil
  end

  -- Dates
  local fmt = text_of(meta["date-format"]) or text_of(meta["dw-date-format"]) or "long"
  local date = text_of(meta.date)
  if date then
    local formatted = L.format_date(date, fmt, loc)
    if formatted then
      meta["date-iso"] = pandoc.MetaString(date)
      meta.date = inlines(formatted)
    end
  end

  -- Generated headings and labels
  local function default(key, term)
    local value = L.term(loc, term)
    if meta[key] == nil and value then meta[key] = inlines(value) end
  end
  if loc.known then
    default("abstract-title", "abstract")
    default("toc-title", "contents")
    default("lof-title", "list-of-figures")
    default("lot-title", "list-of-tables")
  end
  local heading = text_of(meta["dw-bib-heading"])
  if heading and heading ~= "none" and meta["reference-section-title"] == nil then
    default("reference-section-title", heading)
  end
  local terms = {}
  for _, key in ipairs({ "figure", "figures", "table", "tables", "equation", "equations", "section", "sections",
                         "and", "chapter", "page", "by", "the-end", "keywords", "about-words" }) do
    terms[key] = pandoc.MetaString(L.term(loc, key) or key)
  end
  local fig, tbl = text_of(meta["dw-caption-figure"]), text_of(meta["dw-caption-table"])
  if fig then terms.figure, terms.figures = pandoc.MetaString(fig), pandoc.MetaString(fig) end
  if tbl then terms.table, terms.tables = pandoc.MetaString(tbl), pandoc.MetaString(tbl) end
  meta["dw-terms"] = pandoc.MetaMap(terms)
  meta["dw-lang"] = pandoc.MetaString(loc.tag)
  meta["dw-number-group"] = pandoc.MetaString(loc.group or ",")
  doc.meta = meta
  return doc
end
