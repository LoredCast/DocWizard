--[[
diagram.lua: diagrams written as text in fenced code blocks (ARCHITECTURE.md §9.7).

  ```dot                      Graphviz (also ```graphviz)
  digraph { Idea -> Draft -> Paper }
  ```

  ```mermaid                  Mermaid flowcharts (graph / flowchart), drawn by Graphviz
  flowchart LR
    A[Question] --> B{Data?} -->|yes| C(Analysis)
  ```

  ```{.typst .diagram}        Typst drawing code: shapes, grids, math; CeTZ with ```cetz
  #circle(radius: 1cm)
  ```

Everything is drawn offline in the render worker: Graphviz's dot and Typst run here,
in the job's sandbox. The attributes work as for pictures:

  ```{.mermaid #fig:flow caption="How a paper comes about" width=80%}

An identifier starting with fig: numbers the diagram as a figure and lets the text refer to
it as [@fig:flow]; a caption alone makes an unnumbered figure. `.listing` keeps the code
as code (```{.dot .listing}). Markdown outputs keep the code as written.

Runs right after lang.lua, before the layout's filters, so crossref.lua numbers the figures.
]]

local G = require "dwgraphics"

local SANS = "Noto Sans"
local SANS_STACK = "Noto Sans,Helvetica,Arial,sans-serif"
-- The CeTZ release ```cetz blocks import. Like every Typst package, it must be in the
-- worker's package cache (the worker has no network).
local CETZ = "@preview/cetz:0.4.2"

local function has_class(el, name)
  for _, c in ipairs(el.classes) do if c == name then return true end end
  return false
end

-- Graphviz writes the font it was given; viewers without Noto Sans fall back to other sans fonts.
local function widen_fonts(svg)
  return (svg:gsub('font%-family="' .. SANS:gsub(" ", "%%s") .. '"', 'font-family="' .. SANS_STACK .. '"'))
end

local function graphviz(code, fmt)
  local ok, out = pcall(pandoc.pipe, "dot", {
    "-T" .. fmt, "-Gbgcolor=transparent", "-Gfontname=" .. SANS, "-Nfontname=" .. SANS, "-Efontname=" .. SANS,
    "-Gpad=0.05",
  }, code)
  if not ok then
    local msg = G.error_text(out)
    if msg:match("No such file") or msg:match("not found") or msg:match("does not exist") then
      msg = "Graphviz isn't installed in the render worker."
    end
    return nil, msg
  end
  return out
end

-- -- Mermaid flowcharts -------------------------------------------------------------
-- The common part of Mermaid's flowchart syntax, turned into Graphviz's language: directions,
-- node shapes, edge kinds and labels, chains (A --> B --> C), groups (A & B --> C) and
-- subgraphs. classDef, style, click and similar lines are left out.

local SHAPES = {  -- opening bracket -> closing bracket, Graphviz attributes
  { "(((", ")))", 'shape=doublecircle' },
  { "((", "))", 'shape=circle' },
  { "([", "])", 'shape=box, style="rounded,filled"' },
  { "[[", "]]", 'shape=box, peripheries=2' },
  { "[(", ")]", 'shape=cylinder' },
  { "[/", "\\]", 'shape=trapezium' },
  { "[\\", "/]", 'shape=invtrapezium' },
  { "[/", "/]", 'shape=parallelogram' },
  { "[\\", "\\]", 'shape=parallelogram' },
  { "{{", "}}", 'shape=hexagon' },
  { "[", "]", 'shape=box' },
  { "(", ")", 'shape=box, style="rounded,filled"' },
  { "{", "}", 'shape=diamond' },
  { ">", "]", 'shape=cds' },
}
local DIRECTIONS = { TD = "TB", TB = "TB", BT = "BT", LR = "LR", RL = "RL" }

local function dot_string(s)
  s = s:gsub("<br%s*/?>", "\n"):gsub("^%s+", ""):gsub("%s+$", "")
  return '"' .. s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n") .. '"'
end

local function strip_quotes(s)
  s = s:gsub("^%s+", ""):gsub("%s+$", "")
  local inner = s:match('^"(.*)"$')
  return inner or s
end

local Parser = {}
Parser.__index = Parser

local function new_parser()
  return setmetatable({ nodes = {}, order = {}, edges = {}, clusters = {}, stack = {} }, Parser)
end

function Parser:node(id, label, shape)
  local n = self.nodes[id]
  if not n then
    n = { id = id, label = id, attrs = 'shape=box' }
    self.nodes[id] = n
    self.order[#self.order + 1] = id
  end
  local cluster = self.stack[#self.stack]
  if cluster and not n.cluster then
    -- Mentioning a node inside a subgraph puts it there.
    n.cluster = cluster
    cluster.members[#cluster.members + 1] = id
  end
  if label then n.label = label end
  if shape then n.attrs = shape end
  return n
end

-- One node at position i of line s: returns its id and the position after it.
function Parser:read_node(s, i)
  local id_start, id_end = s:find("^[%w_]+", i)
  if not id_start then return nil end
  local id = s:sub(id_start, id_end)
  local j = id_end + 1
  -- Of the shapes that open here, the one whose closing bracket comes first ([/a/] vs [/a\]).
  local best, best_stop
  for _, sh in ipairs(SHAPES) do
    local open, close = sh[1], sh[2]
    if s:sub(j, j + #open - 1) == open then
      local text_start = j + #open
      local stop
      if s:sub(text_start, text_start) == '"' then
        local q = s:find('"', text_start + 1, true)
        if q and s:sub(q + 1, q + #close) == close then stop = q + 1 end
      end
      stop = stop or s:find(close, text_start, true)
      if stop and (not best_stop or stop < best_stop) then best, best_stop = sh, stop end
    end
  end
  if best then
    self:node(id, strip_quotes(s:sub(j + #best[1], best_stop - 1)), best[3])
    return id, best_stop + #best[2]
  end
  self:node(id)
  return id, j
end

-- The nodes of a group: A & B & C.
function Parser:read_group(s, i)
  local ids = {}
  while true do
    i = s:find("%S", i) or #s + 1
    local id, j = self:read_node(s, i)
    if not id then return ids, i end
    ids[#ids + 1] = id
    local k = s:find("%S", j) or #s + 1
    if s:sub(k, k) ~= "&" then return ids, j end
    i = k + 1
  end
end

local CLOSERS = { "-->", "---", "==>", "===", ".->", "-.-", "--x", "--o", "==x", "==o" }

-- An edge at position i: returns its attributes and the position after it, or nil.
function Parser:read_edge(s, i)
  i = s:find("%S", i)
  if not i then return nil end
  local start, stop, head_back, body, head = s:find("^(<?)([%-=%.]+)([>ox]?)", i)
  if not start or #body < 2 and head == "" then return nil end
  local label
  local token = head_back .. body .. head
  if head == "" and (body == "--" or body == "==" or body == "-.") and s:sub(stop + 1, stop + 1):match("%s") then
    -- A -- text --> B
    local best, best_end
    for _, c in ipairs(CLOSERS) do
      local a, b = s:find(c, stop + 1, true)
      if a and (not best or a < best) then best, best_end = a, b end
    end
    if not best then return nil end
    label = s:sub(stop + 1, best - 1)
    local tail_end = best_end
    while s:sub(tail_end + 1, tail_end + 1):match("[%-=%.>]") do tail_end = tail_end + 1 end
    token = token .. s:sub(best, tail_end)
    stop = tail_end
  end
  local j = stop + 1
  local k = s:find("%S", j)
  if k and s:sub(k, k) == "|" then
    local close = s:find("|", k + 1, true)
    if close then
      label = s:sub(k + 1, close - 1)
      j = close + 1
    end
  end
  local attrs = {}
  if token:find("%.") then attrs[#attrs + 1] = "style=dashed" end
  if token:find("=") then attrs[#attrs + 1] = "penwidth=2" end
  local last = token:sub(-1)
  if last == ">" then
    if token:sub(1, 1) == "<" then attrs[#attrs + 1] = "dir=both" end
  elseif last == "o" then
    attrs[#attrs + 1] = "arrowhead=odot"
  elseif last == "x" then
    attrs[#attrs + 1] = "arrowhead=tee"
  else
    attrs[#attrs + 1] = "arrowhead=none"
  end
  if label and strip_quotes(label) ~= "" then attrs[#attrs + 1] = "label=" .. dot_string(strip_quotes(label)) end
  return attrs, j
end

function Parser:statement(s)
  s = s:gsub("^%s+", ""):gsub("%s+$", "")
  if s == "" or s:match("^%%%%") then return end
  local word = s:match("^(%a+)")
  if word == "subgraph" then
    local rest = s:sub(10):gsub("^%s+", "")
    local id, title = rest:match("^([%w_]+)%s*%[(.*)%]$")
    if not id then id, title = rest, rest end
    local cluster = { id = "cluster_" .. (#self.clusters + 1), label = strip_quotes(title or ""), members = {},
                      parent = self.stack[#self.stack] }
    self.clusters[#self.clusters + 1] = cluster
    self.stack[#self.stack + 1] = cluster
    return
  end
  if word == "end" and s == "end" then
    self.stack[#self.stack] = nil
    return
  end
  if word == "direction" or word == "classDef" or word == "class" or word == "style" or word == "linkStyle"
      or word == "click" or word == "accTitle" or word == "accDescr" then
    return
  end
  local left, i = self:read_group(s, 1)
  if #left == 0 then return end
  while true do
    local attrs, j = self:read_edge(s, i)
    if not attrs then return end
    local right, k = self:read_group(s, j)
    if #right == 0 then return end
    for _, a in ipairs(left) do
      for _, b in ipairs(right) do
        self.edges[#self.edges + 1] = { a, b, attrs }
      end
    end
    left, i = right, k
  end
end

function Parser:dot(rankdir)
  local out = { "digraph mermaid {", '  rankdir=' .. rankdir .. ';',
                '  node [style=filled, fillcolor="#f3f4f6", color="#4b5563", fontsize=12, margin="0.15,0.06"];',
                '  edge [color="#4b5563", fontsize=10];' }
  local function emit_cluster(c, indent)
    out[#out + 1] = indent .. "subgraph " .. c.id .. " {"
    out[#out + 1] = indent .. '  label=' .. dot_string(c.label) .. '; style="rounded,dashed"; color="#9ca3af";'
    for _, m in ipairs(c.members) do
      out[#out + 1] = indent .. "  " .. dot_string(m) .. ";"
    end
    for _, child in ipairs(self.clusters) do
      if child.parent == c then emit_cluster(child, indent .. "  ") end
    end
    out[#out + 1] = indent .. "}"
  end
  for _, id in ipairs(self.order) do
    local n = self.nodes[id]
    local attrs = n.attrs
    if not attrs:find("style=") then attrs = attrs .. ', style=filled' end
    out[#out + 1] = "  " .. dot_string(id) .. " [label=" .. dot_string(n.label) .. ", " .. attrs .. "];"
  end
  for _, c in ipairs(self.clusters) do
    if not c.parent then emit_cluster(c, "  ") end
  end
  for _, e in ipairs(self.edges) do
    out[#out + 1] = "  " .. dot_string(e[1]) .. " -> " .. dot_string(e[2]) .. " [" .. table.concat(e[3], ", ") .. "];"
  end
  out[#out + 1] = "}"
  return table.concat(out, "\n")
end

-- Mermaid source to Graphviz source, or nil and why not.
local function mermaid_to_dot(code)
  local lines = {}
  for line in (code .. "\n"):gmatch("(.-)\r?\n") do
    line = line:gsub("%%%%.*$", "")
    for part in (line .. ";"):gmatch("(.-);") do lines[#lines + 1] = part end
  end
  local first, header = nil, nil
  for idx, l in ipairs(lines) do
    if l:match("%S") then first, header = idx, l:gsub("^%s+", "") break end
  end
  if not header then return nil, "The diagram is empty." end
  local kind, dir = header:match("^(%a+)%s*(%u*)")
  if kind ~= "graph" and kind ~= "flowchart" then
    return nil, "Only Mermaid flowcharts (graph, flowchart) can be drawn; this is a " .. (kind or "?") .. " diagram."
  end
  local p = new_parser()
  local rest = header:gsub("^%a+%s*%u*", "", 1)
  p:statement(rest)
  for idx = first + 1, #lines do p:statement(lines[idx]) end
  return p:dot(DIRECTIONS[dir] or "TB")
end

-- -- Typst drawings -----------------------------------------------------------------
local function typst_source(code, cetz)
  if cetz then
    return '#import "' .. CETZ .. '"\n#cetz.canvas({\n  import cetz.draw: *\n' .. code .. '\n})'
  end
  return code
end

-- -- The filter ------------------------------------------------------------------
local function make(cb)
  local classes = cb.classes
  local kind
  if has_class(cb, "listing") then return nil end
  if has_class(cb, "dot") or has_class(cb, "graphviz") then kind = "dot"
  elseif has_class(cb, "mermaid") then kind = "mermaid"
  elseif has_class(cb, "cetz") then kind = "cetz"
  elseif has_class(cb, "typst") and has_class(cb, "diagram") then kind = "typst"
  else return nil end
  if G.keeps_source() then return nil end
  local target = G.target()
  local id = cb.identifier
  local caption = G.caption_from(cb.attr)
  local width = cb.attributes["width"] or ""
  local alt = cb.attributes["alt"] or (caption and pandoc.utils.stringify(caption)) or "Diagram"
  if alt == "" then alt = "Diagram" end
  local inline
  if kind == "dot" or kind == "mermaid" then
    local code = cb.text
    if kind == "mermaid" then
      local dot, why = mermaid_to_dot(code)
      if not dot then return G.problem("Mermaid diagram", why) end
      code = dot
    end
    local svg, err = graphviz(code, "svg")
    if not svg then return G.problem(kind == "dot" and "Graphviz diagram" or "Mermaid diagram", err) end
    svg = widen_fonts(svg)
    local pdf
    if target == "latex" then pdf = graphviz(code, "pdf") end
    inline = G.svg_inline(svg, { width = width, alt = alt, pdf = pdf })
  else
    local source = typst_source(cb.text, kind == "cetz")
    if target == "typst" then
      -- Drawn with the document itself, in its fonts.
      local code = "#box[\n" .. source .. "\n]"
      if width ~= "" then code = "#box(width: " .. width .. ")[\n" .. source .. "\n]" end
      inline = pandoc.RawInline("typst", code)
    else
      local page = "#set page(width: auto, height: auto, margin: 4pt, fill: none)\n#set text(font: (\"" .. SANS
        .. "\", \"Libertinus Serif\"))\n"
      local fmt = target == "latex" and "pdf" or "svg"
      local data, err = G.typst(page .. source, fmt)
      if not data then return G.problem("Typst diagram", err) end
      if fmt == "pdf" then
        local svg = G.typst(page .. source, "svg") or ""
        inline = G.svg_inline(svg, { width = width, alt = alt, pdf = data })
      else
        inline = G.svg_inline(data, { width = width, alt = alt })
      end
    end
  end
  local kept = {}
  for _, c in ipairs(classes) do
    if c ~= "dot" and c ~= "graphviz" and c ~= "mermaid" and c ~= "typst" and c ~= "cetz" and c ~= "diagram" then
      kept[#kept + 1] = c
    end
  end
  table.insert(kept, 1, "diagram")
  return G.block(inline, caption, id, kept)
end

return {
  {
    Meta = function(meta)
      G.read_meta(meta)
      -- Server paths have no place in outputs that keep the metadata (Markdown exports).
      meta["dw-font-paths"] = nil
      return meta
    end,
  },
  { CodeBlock = make },
}
