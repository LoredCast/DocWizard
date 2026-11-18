--[[
screenplay.lua: screenplays written as plain Markdown (ARCHITECTURE.md §9.3). The
rules follow Fountain (fountain.io), so a script reads naturally as text:

  INT. HARBOUR OFFICE - NIGHT          a scene heading: INT., EXT., INT./EXT., I/E,
                                       EST., INNEN, AUSSEN, or a line that starts
                                       with a dot (.FLASHBACK)
  Rain hammers the window.             action: everything else; line breaks stay

  MARA                                 a character: a line in capitals (or @Name),
  (quietly)                            directly followed by dialogue; lines in
  You came back.                       parentheses are parentheticals

  CUT TO:                              a transition: capitals ending in TO: or AUF:,
                                       or > CUT TO BLACK.
  > THE END <                          centred text
  # ACT ONE                            level 1 headings: act and sequence titles
  ## INT. KITCHEN - DAY                other headings are scene headings too

  BRICK ^                              dual dialogue: a cue ending in ^ speaks at the
  Screw retirement.                    same time as the speech before, side by side

Explicit blocks also work: ::: dialogue, ::: character, ::: parenthetical,
::: transition, ::: scene-heading, ::: action, ::: centered.

A character who speaks again after action in the same scene gets "(CONT'D)" after the
name (dw-contd; empty turns it off). In Typst, a speech that runs over a page break gets
"(MORE)" under it and the name with "(CONT'D)" on the next page (the template's dw-speech).

Typst output calls the dw-scene, dw-character, … functions of the default
template, which read their positions from the layout's script.* settings.
Other formats get divs with these classes and a custom-style for Word.
]]

local stringify = pandoc.utils.stringify
local TYPST = FORMAT == "typst"

local SCENE = { "INT%.?/EXT", "EXT%.?/INT", "I/E", "INT", "EXT", "EST", "INNEN", "AUSSEN", "AUẞEN", "INNEN/AUSSEN" }
local STYLES = {
  ["scene-heading"] = "Scene Heading", action = "Action", character = "Character",
  parenthetical = "Parenthetical", dialogue = "Dialogue", transition = "Transition", centered = "Centered",
  act = "Act",
}

local split, numbered = false, false
local scene_no = 0
local contd = "(CONT'D)"

-- Typst string literal
local function tstr(text) return '"' .. text:gsub("\\", "\\\\"):gsub('"', '\\"') .. '"' end

-- Typst calls wrap the inlines themselves, not their written text, so citations, notes and
-- quotes inside a script are still processed by the filters and citeproc that run later.
local function raw(text) return pandoc.RawInline("typst", text) end

-- Lines of a paragraph, as lists of inlines.
local function lines_of(inlines)
  local lines, cur = {}, pandoc.Inlines({})
  for _, el in ipairs(inlines) do
    if el.t == "SoftBreak" or el.t == "LineBreak" then
      lines[#lines + 1] = cur
      cur = pandoc.Inlines({})
    else
      cur:insert(el)
    end
  end
  lines[#lines + 1] = cur
  return lines
end

local function is_upper(text)
  local letters = text:gsub("%b()", ""):gsub("[^%a\128-\255]", "")
  return letters ~= "" and pandoc.text.upper(text) == text
end

local function is_scene(text)
  if text:match("^%.[%w\128-\255]") and not text:match("^%.%.") then return true end
  local up = pandoc.text.upper(text)
  for _, prefix in ipairs(SCENE) do
    if up:match("^" .. prefix .. "[%.%s/]") then return true end
  end
  return false
end

local function is_transition(text)
  return is_upper(text) and (text:match("TO:$") or text:match("AUF:$") or text:match("^FADE OUT") or
    text:match("^ABBLENDE"))
end

local function strip_lead(inlines, pattern)
  local first = inlines[1]
  if first and first.t == "Str" then
    local rest = first.text:gsub(pattern, "", 1)
    if rest == "" then
      inlines:remove(1)
      if inlines[1] and inlines[1].t == "Space" then inlines:remove(1) end
    else
      first.text = rest
    end
  end
  return inlines
end

local function element(kind, inlines, extra)
  if TYPST then
    local fn = ({ ["scene-heading"] = "dw-scene", action = "dw-action", character = "dw-character",
                  parenthetical = "dw-parenthetical", dialogue = "dw-dialogue", transition = "dw-transition",
                  centered = "dw-centered", act = "dw-centered" })[kind]
    local out = pandoc.Inlines({ raw("#" .. fn .. "([" .. (kind == "act" and "#underline[#strong[" or "")) })
    out:extend(inlines)
    out:insert(raw((kind == "act" and "]]" or "") .. "]"))
    if extra and extra.right then
      out:insert(raw(", right: ["))
      out:extend(extra.right)
      out:insert(raw("]"))
    end
    if extra and extra.number then out:insert(raw(', number: "' .. extra.number .. '"')) end
    out:insert(raw(")"))
    return pandoc.Plain(out)
  end
  local content = pandoc.Inlines(inlines)
  if extra and extra.number then
    content:insert(1, pandoc.Space())
    content:insert(1, pandoc.Str(extra.number))
  end
  local blocks = { pandoc.Para(content) }
  if extra and extra.right then
    blocks = { pandoc.Para({ pandoc.Span(content), pandoc.Space(), pandoc.Span(extra.right, { class = "right" }) }) }
  end
  return pandoc.Div(blocks, pandoc.Attr("", { kind }, { ["custom-style"] = STYLES[kind] }))
end

local function scene(inlines)
  inlines = strip_lead(pandoc.Inlines(inlines), "^%.")
  scene_no = scene_no + 1
  local extra = { number = numbered and tostring(scene_no) or nil }
  if split then
    -- "Verhörzimmer – innen / Tag": the part after the last dash goes to the right margin.
    for i = #inlines, 1, -1 do
      local el = inlines[i]
      if el.t == "Str" and (el.text == "–" or el.text == "-" or el.text == "—") and i > 1 and i < #inlines then
        local left, right = pandoc.Inlines({}), pandoc.Inlines({})
        for j = 1, i - 1 do left:insert(inlines[j]) end
        for j = i + 1, #inlines do right:insert(inlines[j]) end
        while left[#left] and left[#left].t == "Space" do left:remove(#left) end
        while right[1] and right[1].t == "Space" do right:remove(1) end
        extra.right = right
        inlines = left
        break
      end
    end
  end
  return element("scene-heading", inlines, extra)
end

local function join(lines)
  local out = pandoc.Inlines({})
  for i, line in ipairs(lines) do
    if i > 1 then out:insert(pandoc.LineBreak()) end
    out:extend(line)
  end
  return out
end

local function para(p)
  local lines = lines_of(p.content)
  -- Markdown reads "@McCLANE" as a citation; Fountain means a character cue.
  local lead = lines[1][1]
  if #lines > 1 and #lines[1] == 1 and lead.t == "Cite" and #lead.citations == 1
      and lead.citations[1].mode == "AuthorInText" then
    lines[1] = pandoc.Inlines({ pandoc.Str("@" .. lead.citations[1].id) })
  end
  local first = stringify(lines[1])
  if #lines == 1 then
    if is_scene(first) then return { scene(lines[1]) } end
    if is_transition(first) then return { element("transition", lines[1]) } end
    return { element("action", lines[1]) }
  end
  local forced = first:match("^@")
  if forced or (is_upper(first) and not is_scene(first) and not first:match(":$")) then
    local cue = forced and strip_lead(lines[1], "^@") or lines[1]
    local sp = { cue = cue, parts = {} }
    -- "BRICK ^": dual dialogue with the speech before.
    local last = cue[#cue]
    if last and last.t == "Str" and last.text:match("%^$") then
      last.text = last.text:gsub("%s*%^$", "")
      if last.text == "" then cue:remove(#cue) end
      while cue[#cue] and cue[#cue].t == "Space" do cue:remove(#cue) end
      sp.dual = true
    end
    local speech = {}
    local function flush()
      if #speech > 0 then sp.parts[#sp.parts + 1] = { kind = "dialogue", inlines = join(speech) } end
      speech = {}
    end
    for i = 2, #lines do
      local text = stringify(lines[i])
      if text:match("^%(.*%)$") then
        flush()
        sp.parts[#sp.parts + 1] = { kind = "parenthetical", inlines = lines[i] }
      else
        speech[#speech + 1] = lines[i]
      end
    end
    flush()
    return nil, sp
  end
  if is_scene(first) then
    local out = { scene(lines[1]) }
    local rest = {}
    for i = 2, #lines do rest[#rest + 1] = lines[i] end
    out[#out + 1] = element("action", join(rest))
    return out
  end
  return { element("action", join(lines)) }
end

local function block_quote(bq)
  if #bq.content == 1 and bq.content[1].t == "Para" then
    local inlines = pandoc.Inlines(bq.content[1].content)
    local last = inlines[#inlines]
    if last and last.t == "Str" and last.text:match("<$") then
      last.text = last.text:gsub("%s*<$", "")
      if last.text == "" then inlines:remove(#inlines) end
      while inlines[#inlines] and inlines[#inlines].t == "Space" do inlines:remove(#inlines) end
      return { element("centered", inlines) }
    end
    return { element("transition", inlines) }
  end
  return nil
end

local EXPLICIT = { "scene-heading", "action", "character", "parenthetical", "dialogue", "transition", "centered" }

local function div(d)
  for _, kind in ipairs(EXPLICIT) do
    if d.classes:includes(kind) then
      local inlines = pandoc.Inlines({})
      for i, b in ipairs(d.content) do
        if i > 1 then inlines:insert(pandoc.LineBreak()) end
        if b.content and type(b.content) ~= "string" then inlines:extend(pandoc.utils.blocks_to_inlines({ b })) end
      end
      if kind == "scene-heading" then return { scene(inlines) } end
      return { element(kind, inlines) }
    end
  end
  return nil
end

-- The name a cue is about: without extensions such as (V.O.) or (CONT'D).
local function speaker(cue)
  return pandoc.text.upper(stringify(cue):gsub("%b()", "")):gsub("^%s+", ""):gsub("%s+$", "")
end

local function speech_args(sp)
  local out = pandoc.Inlines({ raw("(cue: [") })
  out:extend(sp.cue)
  out:insert(raw("], parts: ("))
  for _, part in ipairs(sp.parts) do
    out:insert(raw("(kind: " .. tstr(part.kind) .. ", body: ["))
    out:extend(part.inlines)
    out:insert(raw("]),"))
  end
  out:insert(raw("))"))
  return out
end

local function speech_blocks(sp)
  if TYPST then
    local out = pandoc.Inlines({ raw("#dw-speech(") })
    out:extend(speech_args(sp))
    out:insert(raw(")"))
    return { pandoc.Plain(out) }
  end
  local out = { element("character", sp.cue) }
  for _, part in ipairs(sp.parts) do out[#out + 1] = element(part.kind, part.inlines) end
  return out
end

local function dual_blocks(a, b)
  if TYPST then
    local out = pandoc.Inlines({ raw("#dw-dual(") })
    out:extend(speech_args(a))
    out:insert(raw(", "))
    out:extend(speech_args(b))
    out:insert(raw(")"))
    return { pandoc.Plain(out) }
  end
  local function side(sp)
    return pandoc.Div(speech_blocks(sp), pandoc.Attr("", { "speech" }))
  end
  return { pandoc.Div({ side(a), side(b) }, pandoc.Attr("", { "dual-dialogue" }, { ["custom-style"] = "Dual Dialogue" })) }
end

function Pandoc(doc)
  local meta = doc.meta
  split = meta["dw-scene-split"] == true or (meta["dw-scene-split"] and stringify(meta["dw-scene-split"]) == "true")
  numbered = meta["dw-scene-numbers"] == true
    or (meta["dw-scene-numbers"] and stringify(meta["dw-scene-numbers"]) == "true")
  if meta["dw-contd"] ~= nil then contd = stringify(meta["dw-contd"]) end
  local out = pandoc.Blocks({})
  local last_speaker = nil -- who spoke last in this scene
  local prev = nil -- {index, speech} when the block before is a speech
  for _, b in ipairs(doc.blocks) do
    local repl, sp
    if b.t == "Para" or b.t == "Plain" then repl, sp = para(b)
    elseif b.t == "Header" then
      repl = b.level == 1 and { element("act", b.content) } or { scene(b.content) }
    elseif b.t == "BlockQuote" then repl = block_quote(b)
    elseif b.t == "Div" then repl = div(b)
    end
    if sp then
      local name = speaker(sp.cue)
      if sp.dual and prev then
        for _ = 1, prev[3] do out:remove(#out) end
        for _, r in ipairs(dual_blocks(prev[2], sp)) do out:insert(r) end
        prev = nil
      else
        if contd ~= "" and name == last_speaker and not stringify(sp.cue):find(contd, 1, true) then
          sp.cue:insert(pandoc.Space())
          sp.cue:insert(pandoc.Str(contd))
        end
        local blocks = speech_blocks(sp)
        for _, r in ipairs(blocks) do out:insert(r) end
        prev = { #out, sp, #blocks }
      end
      last_speaker = name
    else
      if repl then
        for _, r in ipairs(repl) do
          out:insert(r)
          local cls = r.t == "Div" and r.classes[1] or nil
          if cls == "scene-heading" or cls == "act" or cls == "transition" then last_speaker = nil end
          if TYPST and r.t == "Plain" then
            local head = r.content[1]
            if head and head.t == "RawInline" and (head.text:match("^#dw%-scene") or head.text:match("^#dw%-transition")
                or head.text:match("^#dw%-centered")) then
              last_speaker = nil
            end
          end
        end
      else
        out:insert(b)
      end
      prev = nil
    end
  end
  doc.blocks = out
  return doc
end
