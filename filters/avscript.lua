--[[
avscript.lua: two-column A/V scripts for documentaries, video essays and explainers
(ARCHITECTURE.md §9.3). Picture on the left, sound on the right, one row per shot:

  VIDEO: Wide shot of the harbour at dawn. Gulls.
  AUDIO: SFX: gulls, distant engine.

  NARRATOR (V.O.): Every morning the boats go out.

A paragraph that starts with VIDEO: (or BILD:) opens the picture cell of a new
row, AUDIO: (or TON:) the sound cell. Paragraphs without a prefix continue the
cell above. Headings and other blocks end the table and stand across both
columns, so sequences can have titles.

Typst output calls dw-av from the default template; other formats get a table
with the class av. The layout's script.* settings name and size the columns.
]]

local stringify = pandoc.utils.stringify
local TYPST = FORMAT == "typst"

local PREFIX = { video = "video", bild = "video", audio = "audio", ton = "audio" }

local labels = { video = "VIDEO", audio = "AUDIO" }
local numbered = true

local function prefix_of(block)
  if block.t ~= "Para" and block.t ~= "Plain" then return nil end
  local first = block.content[1]
  if not first or first.t ~= "Str" then return nil end
  local word = first.text:match("^(%a+):$")
  local kind = word and PREFIX[pandoc.text.lower(word)]
  if not kind then return nil end
  local content = pandoc.Inlines(block.content)
  content:remove(1)
  while content[1] and (content[1].t == "Space" or content[1].t == "SoftBreak") do content:remove(1) end
  return kind, content
end

local function typst_cell(blocks)
  return "[" .. pandoc.write(pandoc.Pandoc(blocks), "typst"):gsub("%s+$", "") .. "]"
end

local function table_of(rows)
  if TYPST then
    local parts = {}
    for _, row in ipairs(rows) do
      parts[#parts + 1] = "(" .. typst_cell(row.video) .. ", " .. typst_cell(row.audio) .. ")"
    end
    return pandoc.RawBlock("typst", "#dw-av((" .. table.concat(parts, ", ") .. ",))")
  end
  local headers = { { pandoc.Plain(labels.video) }, { pandoc.Plain(labels.audio) } }
  local aligns = { pandoc.AlignDefault, pandoc.AlignDefault }
  local widths = { 0.45, 0.55 }
  local body = {}
  for i, row in ipairs(rows) do
    body[#body + 1] = { row.video, row.audio }
    if numbered then table.insert(body[#body], 1, { pandoc.Plain(tostring(i)) }) end
  end
  if numbered then
    table.insert(headers, 1, {})
    table.insert(aligns, 1, pandoc.AlignDefault)
    widths = { 0.06, 0.42, 0.52 }
  end
  local simple = pandoc.SimpleTable({}, aligns, widths, headers, body)
  local tbl = pandoc.utils.from_simple_table(simple)
  tbl.attr = pandoc.Attr("", { "av" })
  return tbl
end

function Pandoc(doc)
  local meta = doc.meta
  if meta["dw-av-video"] then labels.video = stringify(meta["dw-av-video"]) end
  if meta["dw-av-audio"] then labels.audio = stringify(meta["dw-av-audio"]) end
  if meta["dw-av-numbers"] ~= nil then
    numbered = meta["dw-av-numbers"] == true or stringify(meta["dw-av-numbers"]) == "true"
  end
  local out = pandoc.Blocks({})
  local rows, row, cell = {}, nil, nil
  local function flush()
    if #rows > 0 then out:insert(table_of(rows)) end
    rows, row, cell = {}, nil, nil
  end
  for _, b in ipairs(doc.blocks) do
    local kind, content = prefix_of(b)
    if kind then
      if not row or (kind == "video" and (#row.video > 0 or #row.audio > 0)) or (kind == "audio" and #row.audio > 0) then
        row = { video = {}, audio = {} }
        rows[#rows + 1] = row
      end
      cell = kind
      if #content > 0 then table.insert(row[kind], pandoc.Para(content)) end
    elseif row and cell and (b.t == "Para" or b.t == "Plain" or b.t == "BulletList" or b.t == "OrderedList"
        or b.t == "BlockQuote" or b.t == "LineBlock") then
      table.insert(row[cell], b)
    else
      flush()
      out:insert(b)
    end
  end
  flush()
  doc.blocks = out
  return doc
end
