--[[
wordcount.lua: word count in exports, using the same rules as the editor
(ARCHITECTURE.md §6.3): body text only, without front matter, citation keys,
code and comments. Footnotes are counted separately. The counts are available
to templates as $wordcount$ and $notewordcount$.
]]

local body, notes = 0, 0
local in_note = false

local function count_words(text)
  local n = 0
  for _ in text:gmatch("[%w\128-\255][%w\128-\255'%-]*") do n = n + 1 end
  return n
end

local counter = {
  traverse = "topdown",
  Str = function(el)
    local n = count_words(el.text)
    if in_note then notes = notes + n else body = body + n end
  end,
  Code = function() return {}, false end,
  CodeBlock = function() return {}, false end,
  RawBlock = function() return {}, false end,
  RawInline = function() return {}, false end,
}

return {
  {
    Pandoc = function(doc)
      for _, block in ipairs(doc.blocks) do
        pandoc.walk_block(block, {
          traverse = "topdown",
          Note = function(note)
            in_note = true
            pandoc.walk_block(pandoc.Div(note.content), counter)
            in_note = false
            return note, false
          end,
          Cite = function(el) return el, false end,
          Str = counter.Str,
          Code = counter.Code,
          CodeBlock = counter.CodeBlock,
          RawBlock = counter.RawBlock,
          RawInline = counter.RawInline,
        })
      end
      doc.meta.wordcount = tostring(body)
      doc.meta.notewordcount = tostring(notes)
      return doc
    end,
  },
}
