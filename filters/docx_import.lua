--[[
docx_import.lua: Word comments and tracked changes for DocWizard's DOCX import (ARCHITECTURE.md §8.11).

Pandoc reads a .docx with --track-changes=all; this filter turns its comment and change
spans into markers the app finds in the Markdown (private-use characters, which the
Markdown writer leaves alone), and appends what they say (authors, dates, comment text)
as JSON in a final HTML comment. The app removes both and creates comment threads and
suggestions at the marked places.
]]

local O, C = "\u{E000}", "\u{E001}"
local stringify = pandoc.utils.stringify
local comments, changes = {}, {}

local function mark(text) return pandoc.Str(O .. text .. C) end

function Span(span)
  local cls = span.classes
  local attr = span.attributes
  if cls:includes("comment-start") then
    local id = attr.id or tostring(#comments)
    comments[#comments + 1] = { id = id, author = attr.author or "", date = attr.date or "",
                                text = stringify(span.content) }
    return mark("C" .. id)
  elseif cls:includes("comment-end") then
    return mark("/C" .. (attr.id or ""))
  elseif cls:includes("insertion") or cls:includes("deletion") then
    local kind = cls:includes("insertion") and "I" or "D"
    changes[#changes + 1] = { kind = kind, author = attr.author or "", date = attr.date or "" }
    local n = tostring(#changes)
    local out = pandoc.Inlines({ mark(kind .. n) })
    out:extend(span.content)
    out:insert(mark("/" .. kind .. n))
    return out
  elseif cls:includes("paragraph-insertion") or cls:includes("paragraph-deletion") then
    return span.content
  end
end

function Pandoc(doc)
  doc = doc:walk({ Span = Span })
  local data = pandoc.json.encode({ comments = comments, changes = changes })
  data = data:gsub(">", "\\u003e"):gsub("<", "\\u003c")
  doc.blocks:insert(pandoc.RawBlock("markdown", "<!-- dw-import " .. data .. " -->"))
  return doc
end

return { { Pandoc = Pandoc } }
