--[[
wordcount.lua: word count in exports, using the same rules as the editor
(ARCHITECTURE.md §6.3): body text only, without front matter, citation keys,
code and comments. Footnotes are counted separately. The counts are available
to templates as $wordcount$ and $notewordcount$.

$word-count$ is the rounded count a manuscript's first page shows, in the
document's language ("about 4,300 words"): to the nearest hundred, and to the
nearest 500 from novella length (17,500 words) on, as Shunn's Modern
Manuscript Format asks. The document can set word-count: itself.
]]

local function rounded(n)
  local step = n >= 17500 and 500 or 100
  local r = math.floor((n + step / 2) / step) * step
  if r == 0 then r = n end
  return r
end

local function grouped(n, sep)
  local s = tostring(math.floor(n))
  local head = #s % 3
  local parts = {}
  if head > 0 then parts[1] = s:sub(1, head) end
  for i = head + 1, #s, 3 do parts[#parts + 1] = s:sub(i, i + 2) end
  return table.concat(parts, sep)
end

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
      if doc.meta["word-count"] == nil then
        local meta = doc.meta
        local sep = meta["dw-number-group"] and pandoc.utils.stringify(meta["dw-number-group"]) or ","
        local pattern = meta["dw-terms"] and meta["dw-terms"]["about-words"]
        pattern = pattern and pandoc.utils.stringify(pattern) or "about {n} words"
        local text = pattern:gsub("{n}", (grouped(rounded(body), sep):gsub("%%", "%%%%")))
        doc.meta["word-count"] = pandoc.MetaString(text)
      end
      return doc
    end,
  },
}
