--[[
crossref.lua: numbered figures, tables, equations and sections (ARCHITECTURE.md §9.7).

Label things with an identifier that starts with a prefix:

  ![A map](map.png){#fig:map}
  : Results {#tbl:results}            (table caption)
  $$ E = mc^2 $$ {#eq:energy}
  # Method {#sec:method}

and reference them like citations: [@fig:map], @tbl:results, [@eq:energy; @sec:method].
Runs before citeproc, which then only sees real citations. Replaces
pandoc-crossref, which has to match the exact pandoc version.
]]

local TYPST = FORMAT == "typst"

local names = {
  fig = { "Figure", "Figures" },
  tbl = { "Table", "Tables" },
  eq = { "Equation", "Equations" },
  sec = { "Section", "Sections" },
}
local conj = { word = "and", serial = true }
local separator = ":"

-- Words in the document's language, from lang.lua (which runs first).
local function read_terms(meta)
  local terms = meta["dw-terms"]
  if not terms then return end
  local function term(key, fallback)
    return terms[key] and pandoc.utils.stringify(terms[key]) or fallback
  end
  for kind, keys in pairs({ fig = { "figure", "figures" }, tbl = { "table", "tables" },
                            eq = { "equation", "equations" }, sec = { "section", "sections" } }) do
    names[kind] = { term(keys[1], names[kind][1]), term(keys[2], names[kind][2]) }
  end
  conj.word = term("and", "and")
  if meta["dw-caption-separator"] then
    separator = pandoc.utils.stringify(meta["dw-caption-separator"]):gsub("%s+$", "")
  end
  local lang = meta["dw-lang"] and pandoc.utils.stringify(meta["dw-lang"]) or "en"
  conj.serial = lang:match("^en") ~= nil
end

local numbers = {}
local counters = { fig = 0, tbl = 0, eq = 0 }
local section = {}

local function prefix_of(id)
  local p = id and id:match("^(%a+):")
  if p and names[p] then return p end
  return nil
end

local function caption_prefix(kind, n)
  return { pandoc.Str(names[kind][1]), pandoc.Space(), pandoc.Str(tostring(n) .. separator), pandoc.Space() }
end

local function prepend_caption(caption, kind, n)
  -- caption.long is a list of blocks; add the label to the first one.
  local long = caption.long
  if #long == 0 then
    long:insert(pandoc.Plain({}))
  end
  local first = long[1]
  if first.content then
    local new = pandoc.Inlines(caption_prefix(kind, n))
    new:extend(first.content)
    first.content = new
  end
  caption.long = long
  return caption
end

local number_pass = {
  traverse = "topdown",
  Header = function(h)
    local level = h.level
    section[level] = (section[level] or 0) + 1
    for l = level + 1, 9 do section[l] = nil end
    if prefix_of(h.identifier) == "sec" then
      local parts = {}
      for l = 1, level do parts[#parts + 1] = tostring(section[l] or 0) end
      numbers[h.identifier] = table.concat(parts, ".")
    end
  end,
  Figure = function(fig)
    local labelled = prefix_of(fig.identifier) == "fig"
    -- Typst numbers every figure itself; count the same way so references match.
    if labelled or TYPST then counters.fig = counters.fig + 1 end
    if labelled then
      numbers[fig.identifier] = tostring(counters.fig)
      if not TYPST then
        fig.caption = prepend_caption(fig.caption, "fig", counters.fig)
        return fig
      end
    end
  end,
  Table = function(tbl)
    local labelled = prefix_of(tbl.identifier) == "tbl"
    local captioned = #tbl.caption.long > 0
    if labelled or (TYPST and captioned) then counters.tbl = counters.tbl + 1 end
    if labelled then
      numbers[tbl.identifier] = tostring(counters.tbl)
      if not TYPST then
        tbl.caption = prepend_caption(tbl.caption, "tbl", counters.tbl)
        return tbl
      end
    end
  end,
  Inlines = function(inlines)
    -- Display math followed by {#eq:label}.
    local i = 1
    while i <= #inlines do
      local el = inlines[i]
      if el.t == "Math" and el.mathtype == "DisplayMath" then
        local j = i + 1
        if inlines[j] and inlines[j].t == "Space" then j = j + 1 end
        local label = inlines[j] and inlines[j].t == "Str" and inlines[j].text:match("^{#(eq:[^}]+)}$")
        if label then
          counters.eq = counters.eq + 1
          numbers[label] = tostring(counters.eq)
          local replacement
          if TYPST then
            -- A numbered block equation in Typst, carrying our number and the label.
            local math = pandoc.write(pandoc.Pandoc({ pandoc.Plain({ el }) }), "typst"):gsub("%s+$", "")
            replacement = pandoc.RawInline("typst", string.format(
              '#math.equation(block: true, numbering: _ => "(%d)", %s) <%s>', counters.eq, math, label))
          else
            replacement = pandoc.Span({ el, pandoc.Space(), pandoc.Str("(" .. counters.eq .. ")") },
              pandoc.Attr(label, { "equation" }))
          end
          for _ = i, j do inlines:remove(i) end
          inlines:insert(i, replacement)
        end
      end
      i = i + 1
    end
    return inlines
  end,
}

local function reference(cite)
  local kinds, refs = {}, {}
  for _, c in ipairs(cite.citations) do
    local kind = prefix_of(c.id)
    if not kind then return nil end
    kinds[#kinds + 1] = kind
    refs[#refs + 1] = c.id
  end
  local kind = kinds[1]
  local out = pandoc.Inlines({})
  local same = true
  for _, k in ipairs(kinds) do if k ~= kind then same = false end end
  for i, id in ipairs(refs) do
    if i > 1 then
      if i == #refs then
        if #refs > 2 and conj.serial then out:insert(pandoc.Str(",")) end
        out:insert(pandoc.Space())
        out:insert(pandoc.Str(conj.word))
      else
        out:insert(pandoc.Str(","))
      end
      out:insert(pandoc.Space())
    end
    local k = kinds[i]
    local label = numbers[id] or "??"
    if not same or i == 1 then
      local word = names[k][(same and #refs > 1) and 2 or 1]
      out:insert(pandoc.Str(word))
      out:insert(pandoc.Space())
    end
    out:insert(pandoc.Link({ pandoc.Str(label) }, "#" .. id))
  end
  return out
end

return {
  { Meta = function(meta) read_terms(meta) end },
  number_pass,
  { Cite = reference },
}
