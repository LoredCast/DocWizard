--[[
runin.lua: headings that run into their paragraph (ARCHITECTURE.md §9.3), as APA
levels 4 and 5 ("Indented, Bold, Ending With a Period. Text begins …") and IEEE's
third level ("1) Sophisticated Local Control: Sophisticated local …") ask.

The layout says which levels run in with dw-run-in-3 and dw-run-in-4 (level 4
and below), each "period" or "colon". The heading becomes the start of the
next paragraph: in Typst a #dw-runin(level)[…] call that keeps the heading
counter and numbering in step, elsewhere a span with the class run-in-N.
]]

local stringify = pandoc.utils.stringify
local TYPST = FORMAT == "typst"

local function setting(meta, level)
  local key = level >= 4 and "dw-run-in-4" or "dw-run-in-3"
  local value = meta[key] and stringify(meta[key]) or ""
  if level >= 3 and (value == "period" or value == "colon") then return value end
  return nil
end

local function ends_with_punct(inlines)
  local last = inlines[#inlines]
  return last and last.t == "Str" and last.text:match("[.:?!]$")
end

function Pandoc(doc)
  local meta = doc.meta
  if not meta["dw-run-in-3"] and not meta["dw-run-in-4"] then return nil end
  local out = pandoc.Blocks({})
  local blocks = doc.blocks
  local i = 1
  while i <= #blocks do
    local b = blocks[i]
    local mode = b.t == "Header" and setting(meta, b.level)
    if mode then
      local lead
      if TYPST then
        local body = pandoc.write(pandoc.Pandoc({ pandoc.Plain(b.content) }), "typst"):gsub("%s+$", "")
        local label = b.identifier ~= "" and (" <" .. b.identifier .. ">") or ""
        lead = { pandoc.RawInline("typst", string.format("#dw-runin(%d)[%s]%s", b.level, body, label)) }
      else
        local content = pandoc.Inlines(b.content)
        if not ends_with_punct(content) then content:insert(pandoc.Str(mode == "colon" and ":" or ".")) end
        lead = { pandoc.Span(content, pandoc.Attr(b.identifier, { "run-in", "run-in-" .. math.min(b.level, 4) })),
                 pandoc.Space() }
      end
      local nxt = blocks[i + 1]
      if nxt and nxt.t == "Para" then
        local content = pandoc.Inlines(lead)
        content:extend(nxt.content)
        out:insert(pandoc.Para(content))
        i = i + 2
      else
        out:insert(pandoc.Para(lead))
        i = i + 1
      end
    else
      out:insert(b)
      i = i + 1
    end
  end
  doc.blocks = out
  return doc
end
