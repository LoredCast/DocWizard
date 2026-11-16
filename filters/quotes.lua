--[[
quotes.lua: quotation marks and passages in other languages (ARCHITECTURE.md §9.8).
Runs last, after citeproc.

- "Quotes" typed in Markdown become the marks of the language they are in:
  „…“ in German, «…» in French, ‘…’ in British English. A double-quoted
  passage gets the language's main marks and a single-quoted one its inner
  marks, so nesting follows the author's markup. Every output format shows
  the same marks.
- A layout can ask for single marks first (MHRA) or double marks first with
  dw-quotes: single|double; `quotes:` in the front matter overrides it. It
  applies to the document's main language only.
- Typst: pandoc drops the language of [text]{lang=fr} and writes an unscoped
  `#set text(lang: …)` for divs. Both become scoped `text(lang:, region:)`
  calls here, so hyphenation and Typst's own words follow the passage.
]]

local L = require "dwlocale"
local stringify = pandoc.utils.stringify
local TYPST = FORMAT == "typst"

local DOUBLE = { ["“"] = true, ["”"] = true, ["„"] = true, ["«"] = true, ["»"] = true }

local style = "language"
local main_lang = "en"
local cache = {}

local function marks(tag)
  if cache[tag] then return cache[tag] end
  local loc = L.resolve(tag)
  local q = loc.quotes or { "“", "”", "‘", "’" }
  local outer, inner = { q[1], q[2] }, { q[3], q[4] }
  local first = outer[1]:gsub("\u{202F}", "")
  if loc.lang == main_lang and ((style == "single" and DOUBLE[first]) or (style == "double" and not DOUBLE[first])) then
    outer, inner = inner, outer
  end
  cache[tag] = { DoubleQuote = outer, SingleQuote = inner }
  return cache[tag]
end

local function localizer(tag)
  return {
    Quoted = function(q)
      local m = marks(tag)[q.quotetype] or marks(tag).DoubleQuote
      local out = pandoc.Inlines({ pandoc.Str(m[1]) })
      out:extend(q.content)
      out:insert(pandoc.Str(m[2]))
      return out
    end,
  }
end

-- Quotes inside a passage take the passage's language. Inner passages come first
-- (filters walk bottom-up), so their quotes are already marks when the outer one runs.
local function passage(el)
  local tag = el.attributes.lang
  if tag then
    el.content = el.content:walk(localizer(tag))
    return el
  end
end

local function typst_lang(tag)
  local base = tag:match("^(%a+)") or "en"
  local region = tag:match("%-(%u%u)$")
  local args = string.format('lang: "%s"', base:lower())
  if region then args = args .. string.format(', region: "%s"', region) end
  return args
end

local typst_passages = {
  Span = function(el)
    local tag = el.attributes.lang
    if not tag then return nil end
    local body = pandoc.write(pandoc.Pandoc({ pandoc.Plain(el.content) }), "typst"):gsub("%s+$", "")
    return pandoc.RawInline("typst", "#text(" .. typst_lang(tag) .. ")[" .. body .. "]")
  end,
  Div = function(el)
    local tag = el.attributes.lang
    if not tag then return nil end
    el.attributes.lang = nil
    local body = pandoc.write(pandoc.Pandoc(el.content), "typst"):gsub("%s+$", "")
    return pandoc.RawBlock("typst", "#[\n#set text(" .. typst_lang(tag) .. ")\n" .. body .. "\n]")
  end,
}

function Pandoc(doc)
  local meta = doc.meta
  local main = L.normalize(meta.lang and stringify(meta.lang) or "") or "en-US"
  main_lang = main:match("^(%a+)")
  local chosen = meta.quotes and stringify(meta.quotes) or (meta["dw-quotes"] and stringify(meta["dw-quotes"]))
  if chosen == "single" or chosen == "double" then style = chosen end
  doc.blocks = doc.blocks:walk({ Span = passage, Div = passage })
  doc.blocks = doc.blocks:walk(localizer(main))
  if TYPST then
    doc.blocks = doc.blocks:walk(typst_passages)
  end
  return doc
end
