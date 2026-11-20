--[[
data.lua: tables and charts from the project's data sheets (ARCHITECTURE.md §9.7).

  ::: {.data src="data/results.csv" #tbl:results}
  Results per year
  :::

  ::: {.chart src="data/results.csv" type=bar x=Year y="Revenue,Cost" #fig:revenue}
  Revenue and cost per year
  :::

The div's text is the caption. The table or chart is made from the file each time the
document renders, so it always shows the sheet as it is now; formulas (=SUM(B2:B9)) show
their results, worked out as the editor's grid does (dwsheet.lua).

Tables: columns="Year,Revenue" picks and orders columns, rows="2-10" picks rows of the file
(the header is row 1), header=false when the first row is data, decimals=2 rounds formula
results. Columns of numbers are right-aligned.

Charts (type=bar, hbar, stacked, line, area, scatter or pie): x names the column for the
categories (default: the first), y the columns to draw (default: every other column of
numbers). Also title, xlabel, ylabel, width (80%), height (in pixels of a 640-wide chart),
legend=false, min and max for the value axis. Charts are SVG drawn here, the same in every
output format: no fonts or scripts are needed to show them.

Runs right after lang.lua, before the layout's filters, so crossref.lua numbers tables (tbl:)
and charts (fig:).
]]

local G = require "dwgraphics"
local S = require "dwsheet"
local stringify = pandoc.utils.stringify

local MAX_ROWS = 2000
local MAX_BYTES = 5 * 1024 * 1024
-- Okabe & Ito's colours, distinguishable with the common kinds of colour blindness.
local COLORS = { "#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#000000" }
local FONT = "Noto Sans,Helvetica,Arial,sans-serif"

local function has_class(el, name)
  for _, c in ipairs(el.classes) do if c == name then return true end end
  return false
end

-- A project path: relative, no .., no backslashes.
local function safe_path(p)
  if not p or p == "" or p:match("^/") or p:match("\\") or p:match("^%a:") then return nil end
  for part in p:gmatch("[^/]+") do
    if part == ".." or part == "." then return nil end
  end
  return p
end

local function load(path)
  local f = io.open(path, "rb")
  if not f then return nil, "There is no file " .. path .. " in this project." end
  local text = f:read(MAX_BYTES + 1)
  f:close()
  if #text > MAX_BYTES then return nil, path .. " is larger than 5 MB." end
  local rows = S.parse(text, S.separator(text, path))
  if #rows == 0 then return nil, path .. " is empty." end
  return rows
end

local function xml(s)
  return (tostring(s):gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;"):gsub('"', "&quot;"))
end

-- Columns by header name or letter: "Year,C" -> {1, 3}.
local function pick_columns(spec, header, width)
  if not spec or spec == "" then return nil end
  local out = {}
  for name in spec:gmatch("[^,;]+") do
    name = name:gsub("^%s+", ""):gsub("%s+$", "")
    local found
    for i = 1, width do
      if header and header[i] and header[i]:gsub("^%s+", ""):gsub("%s+$", "") == name then found = i break end
    end
    if not found and name:match("^%u%u?$") then
      local n = S.col_index(name)
      if n <= width then found = n end
    end
    if not found then return nil, "There is no column " .. name .. "." end
    out[#out + 1] = found
  end
  return out
end

local function row_range(spec, count)
  local first, last = 1, count
  if spec and spec ~= "" then
    local a, b = spec:match("^%s*(%d*)%s*%-%s*(%d*)%s*$")
    if a then
      first, last = tonumber(a) or 1, tonumber(b) or count
    elseif spec:match("^%s*%d+%s*$") then
      first, last = tonumber(spec), tonumber(spec)
    end
  end
  return math.max(1, first), math.min(count, last)
end

local function cell_inlines(text)
  if text == "" then return {} end
  if text:match("[%*_`%$@%[%]~^\\<]") then
    local doc = pandoc.read(text, "markdown")
    if doc.blocks[1] and doc.blocks[1].content and #doc.blocks == 1 then return doc.blocks[1].content end
  end
  return pandoc.Inlines(text)
end

local function width_of(rows)
  local w = 0
  for _, r in ipairs(rows) do if #r > w then w = #r end end
  return w
end

-- -- Tables -------------------------------------------------------------------------
local function make_table(div, rows)
  local attrs = div.attributes
  local sheet = S.sheet(rows)
  local width = width_of(rows)
  local has_header = attrs["header"] ~= "false" and attrs["header"] ~= "no"
  local header = has_header and rows[1] or nil
  local cols, err = pick_columns(attrs["columns"], header, width)
  if err then return nil, err end
  if not cols then
    cols = {}
    for i = 1, width do cols[i] = i end
  end
  local decimals = tonumber(attrs["decimals"])
  local first_data = has_header and 2 or 1
  local r1, r2 = row_range(attrs["rows"], #rows)
  r1 = math.max(r1, first_data)
  local truncated = false
  if r2 - r1 + 1 > MAX_ROWS then r2, truncated = r1 + MAX_ROWS - 1, true end
  -- What each cell shows, and which columns hold only numbers.
  local body, numeric = {}, {}
  for j, c in ipairs(cols) do numeric[j] = nil end
  for r = r1, r2 do
    local out = {}
    for j, c in ipairs(cols) do
      local shown = sheet.display(r, c, decimals)
      out[j] = shown
      if shown ~= "" then
        local isnum = S.number(shown) ~= nil
        if numeric[j] == nil then numeric[j] = isnum else numeric[j] = numeric[j] and isnum end
      end
    end
    body[#body + 1] = out
  end
  local aligns = {}
  for j = 1, #cols do aligns[j] = numeric[j] and pandoc.AlignRight or pandoc.AlignDefault end
  local function cell(text, align)
    return pandoc.Cell({ pandoc.Plain(cell_inlines(text)) }, align)
  end
  local head_rows = {}
  if header then
    local hcells = {}
    for j, c in ipairs(cols) do hcells[j] = cell(sheet.display(1, c, decimals), aligns[j]) end
    head_rows[1] = pandoc.Row(hcells)
  end
  local body_rows = {}
  for _, out in ipairs(body) do
    local cells = {}
    for j = 1, #cols do cells[j] = cell(out[j], aligns[j]) end
    body_rows[#body_rows + 1] = pandoc.Row(cells)
  end
  local colspecs = {}
  for j = 1, #cols do colspecs[j] = { aligns[j], pandoc.ColWidthDefault } end
  local caption = {}
  local content = div.content
  if #content > 0 then
    if #content == 1 and content[1].content then
      caption = { long = { pandoc.Plain(content[1].content) } }
    else
      caption = { long = content }
    end
  end
  local tbl = pandoc.Table(caption, colspecs, pandoc.TableHead(head_rows),
    { { attr = pandoc.Attr(), body = body_rows, head = {}, row_head_columns = 0 } },
    pandoc.TableFoot(), pandoc.Attr(div.identifier, { "data-table" }))
  if truncated then
    G.warn(attrs["src"] .. ": only the first " .. MAX_ROWS .. " rows are shown.")
  end
  return tbl
end

-- -- Charts -------------------------------------------------------------------------
-- Round numbers for an axis from lo to hi: about *count* ticks of 1, 2 or 5 × 10^n.
local function nice_ticks(lo, hi, count)
  if lo == hi then
    if lo == 0 then hi = 1 else lo, hi = math.min(0, lo), math.max(0, hi) end
    if lo == hi then hi = lo + 1 end
  end
  local span = hi - lo
  local raw = span / math.max(1, count)
  local mag = 10 ^ math.floor(math.log(raw, 10))
  local step = mag
  for _, m in ipairs({ 1, 2, 2.5, 5, 10 }) do
    if raw <= m * mag + 1e-12 then step = m * mag break end
  end
  local start = math.floor(lo / step + 1e-9) * step
  local stop = math.ceil(hi / step - 1e-9) * step
  local ticks = {}
  local v = start
  while v <= stop + step * 1e-6 and #ticks < 50 do
    ticks[#ticks + 1] = math.abs(v) < step * 1e-9 and 0 or v
    v = v + step
  end
  return ticks, start, stop
end

local function tick_label(v)
  local a = math.abs(v)
  if a >= 1e9 then return S.format(v / 1e9) .. "B" end
  if a >= 1e6 then return S.format(v / 1e6) .. "M" end
  if a >= 1e4 then return S.format(v / 1e3) .. "k" end
  return S.format(v)
end

local function text(x, y, s, opts)
  opts = opts or {}
  return string.format('<text x="%.1f" y="%.1f" font-size="%d"%s%s%s fill="%s">%s</text>', x, y, opts.size or 12,
    opts.anchor and (' text-anchor="' .. opts.anchor .. '"') or "",
    opts.weight and (' font-weight="' .. opts.weight .. '"') or "",
    opts.rotate and string.format(' transform="rotate(%d %.1f %.1f)"', opts.rotate, x, y) or "",
    opts.color or "#333333", xml(s))
end

local function shorten(s, n)
  if utf8.len(s) and utf8.len(s) > n then
    return s:sub(1, (utf8.offset(s, n) or (n + 1)) - 1) .. "…"
  end
  return s
end

-- The chart's data: category labels and series of numbers (nil for empty cells).
local function chart_data(div, rows)
  local attrs = div.attributes
  local sheet = S.sheet(rows)
  local width = width_of(rows)
  local header = rows[1]
  local xcols, err = pick_columns(attrs["x"], header, width)
  if err then return nil, err end
  local xcol = xcols and xcols[1] or 1
  local ycols
  ycols, err = pick_columns(attrs["y"], header, width)
  if err then return nil, err end
  local r1, r2 = row_range(attrs["rows"], #rows)
  r1 = math.max(r1, 2)
  r2 = math.min(r2, r1 + MAX_ROWS - 1)
  if not ycols then
    ycols = {}
    for c = 1, width do
      if c ~= xcol then
        local any = false
        for r = r1, r2 do
          if type(sheet.value(r, c)) == "number" then any = true break end
        end
        if any then ycols[#ycols + 1] = c end
      end
    end
  end
  if #ycols == 0 then return nil, "The sheet has no column of numbers to draw." end
  local data = { labels = {}, xs = {}, series = {} }
  for r = r1, r2 do
    data.labels[#data.labels + 1] = tostring(sheet.display(r, xcol))
    local xv = sheet.value(r, xcol)
    data.xs[#data.xs + 1] = type(xv) == "number" and xv or nil
  end
  for _, c in ipairs(ycols) do
    local s = { name = header[c] and header[c] ~= "" and header[c] or S.col_name(c), values = {} }
    for i, r in ipairs((function() local t = {} for r = r1, r2 do t[#t + 1] = r end return t end)()) do
      local v = sheet.value(r, c)
      s.values[i] = type(v) == "number" and v or nil
    end
    data.series[#data.series + 1] = s
  end
  data.xname = header[xcol] or ""
  return data
end

local function svg_chart(kind, data, attrs)
  local W = 640
  local H = tonumber(attrs["height"]) or 380
  H = math.max(160, math.min(H, 1200))
  local out = {}
  local function add(s) out[#out + 1] = s end
  local title = attrs["title"]
  local legend = attrs["legend"] ~= "false" and (#data.series > 1 or kind == "pie")
  local top = 12 + (title and title ~= "" and 26 or 0) + (legend and kind ~= "pie" and 22 or 0)
  if title and title ~= "" then add(text(W / 2, 26, title, { anchor = "middle", size = 15, weight = "bold" })) end
  local n = #data.labels

  if kind == "pie" then
    local s = data.series[1]
    local total = 0
    for i = 1, n do total = total + math.max(0, s.values[i] or 0) end
    local cx, cy, radius = 230, top + (H - top - 12) / 2, math.min(190, (H - top - 24) / 2)
    if total <= 0 then return nil, "The values to draw a pie from add up to nothing." end
    local angle = -math.pi / 2
    for i = 1, n do
      local v = math.max(0, s.values[i] or 0)
      local frac = v / total
      local color = COLORS[(i - 1) % #COLORS + 1]
      if frac >= 0.9999 then
        add(string.format('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s"/>', cx, cy, radius, color))
      elseif frac > 0 then
        local a2 = angle + frac * 2 * math.pi
        add(string.format('<path d="M%.1f %.1fL%.1f %.1fA%.1f %.1f 0 %d 1 %.1f %.1fZ" fill="%s" stroke="#ffffff" stroke-width="1.5"/>',
          cx, cy, cx + radius * math.cos(angle), cy + radius * math.sin(angle), radius, radius, frac > 0.5 and 1 or 0,
          cx + radius * math.cos(a2), cy + radius * math.sin(a2), color))
        if frac >= 0.04 then
          local mid = (angle + a2) / 2
          add(text(cx + radius * 0.65 * math.cos(mid), cy + radius * 0.65 * math.sin(mid) + 4,
            string.format("%d%%", math.floor(frac * 100 + 0.5)), { anchor = "middle", color = "#ffffff", weight = "bold" }))
        end
        angle = a2
      end
    end
    local ly = top + 10
    for i = 1, math.min(n, 18) do
      local color = COLORS[(i - 1) % #COLORS + 1]
      add(string.format('<rect x="450" y="%.1f" width="12" height="12" fill="%s"/>', ly, color))
      add(text(468, ly + 10, shorten(data.labels[i], 22) .. " (" .. S.format(s.values[i] or 0) .. ")"))
      ly = ly + 20
    end
    return out, W, H
  end

  -- Axes charts.
  local horizontal = kind == "hbar"
  local stacked = kind == "stacked"
  local lo, hi = math.huge, -math.huge
  if stacked then
    for i = 1, n do
      local pos, neg = 0, 0
      for _, s in ipairs(data.series) do
        local v = s.values[i] or 0
        if v >= 0 then pos = pos + v else neg = neg + v end
      end
      lo, hi = math.min(lo, neg), math.max(hi, pos)
    end
  else
    for _, s in ipairs(data.series) do
      for i = 1, n do
        local v = s.values[i]
        if v then lo, hi = math.min(lo, v), math.max(hi, v) end
      end
    end
  end
  if lo == math.huge then return nil, "The columns to draw have no numbers." end
  if kind ~= "line" and kind ~= "scatter" then lo, hi = math.min(lo, 0), math.max(hi, 0) end
  lo = tonumber(attrs["min"]) or lo
  hi = tonumber(attrs["max"]) or hi
  local ticks, vmin, vmax = nice_ticks(lo, hi, 5)
  if tonumber(attrs["min"]) then vmin = tonumber(attrs["min"]) end
  if tonumber(attrs["max"]) then vmax = tonumber(attrs["max"]) end

  -- Scatter plots need numbers on the x axis too.
  local xticks, xmin, xmax
  if kind == "scatter" then
    local xl, xh = math.huge, -math.huge
    for i = 1, n do
      local x = data.xs[i]
      if x then xl, xh = math.min(xl, x), math.max(xh, x) end
    end
    if xl == math.huge then return nil, "A scatter plot needs numbers in the x column." end
    xticks, xmin, xmax = nice_ticks(xl, xh, 6)
  end

  local longest = 0
  for i = 1, n do longest = math.max(longest, utf8.len(data.labels[i]) or #data.labels[i]) end
  local left = horizontal and math.min(170, 14 + 7 * math.min(longest, 24)) or 56
  if attrs["ylabel"] and not horizontal then left = left + 18 end
  local rotate = not horizontal and kind ~= "scatter" and n > 0 and (longest * 7 > (W - left - 20) / n)
  local bottom = horizontal and 34 or (rotate and math.min(110, 18 + 6 * math.min(longest, 18)) or 34)
  if attrs["xlabel"] then bottom = bottom + 18 end
  local plot = { x = left, y = top + 6, w = W - left - 20, h = H - top - 6 - bottom }

  local function vpos(v)  -- value -> pixel along the value axis
    local f = (v - vmin) / (vmax - vmin)
    if horizontal then return plot.x + f * plot.w end
    return plot.y + plot.h - f * plot.h
  end

  -- Grid and value axis.
  for _, tv in ipairs(ticks) do
    if tv >= vmin - 1e-9 and tv <= vmax + 1e-9 then
      local p = vpos(tv)
      if horizontal then
        add(string.format('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#e5e7eb"/>', p, plot.y, p, plot.y + plot.h))
        add(text(p, plot.y + plot.h + 16, tick_label(tv), { anchor = "middle", size = 11, color = "#555555" }))
      else
        add(string.format('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#e5e7eb"/>', plot.x, p, plot.x + plot.w, p))
        add(text(plot.x - 6, p + 4, tick_label(tv), { anchor = "end", size = 11, color = "#555555" }))
      end
    end
  end
  local zero = (vmin <= 0 and vmax >= 0) and vpos(0) or nil

  local function category_center(i)
    local band = (horizontal and plot.h or plot.w) / math.max(1, n)
    return (horizontal and plot.y or plot.x) + band * (i - 0.5), band
  end

  if kind == "scatter" then
    local function xpos(x) return plot.x + (x - xmin) / (xmax - xmin) * plot.w end
    for _, tx in ipairs(xticks) do
      local p = xpos(tx)
      add(string.format('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#f0f1f3"/>', p, plot.y, p, plot.y + plot.h))
      add(text(p, plot.y + plot.h + 16, tick_label(tx), { anchor = "middle", size = 11, color = "#555555" }))
    end
    for si, s in ipairs(data.series) do
      local color = COLORS[(si - 1) % #COLORS + 1]
      for i = 1, n do
        if data.xs[i] and s.values[i] then
          add(string.format('<circle cx="%.1f" cy="%.1f" r="3.5" fill="%s" fill-opacity="0.85"/>', xpos(data.xs[i]),
            vpos(s.values[i]), color))
        end
      end
    end
  elseif kind == "line" or kind == "area" then
    for si, s in ipairs(data.series) do
      local color = COLORS[(si - 1) % #COLORS + 1]
      local pts = {}
      for i = 1, n do
        if s.values[i] then
          local c = category_center(i)
          pts[#pts + 1] = string.format("%.1f,%.1f", c, vpos(s.values[i]))
        end
      end
      if #pts > 0 then
        if kind == "area" then
          local first = category_center(1)
          local last = category_center(n)
          local base = zero or (plot.y + plot.h)
          add(string.format('<polygon points="%.1f,%.1f %s %.1f,%.1f" fill="%s" fill-opacity="0.18"/>', first, base,
            table.concat(pts, " "), last, base, color))
        end
        add(string.format('<polyline points="%s" fill="none" stroke="%s" stroke-width="2.5" stroke-linejoin="round"/>',
          table.concat(pts, " "), color))
        if n <= 40 then
          for _, p in ipairs(pts) do
            local x, y = p:match("([^,]+),(.+)")
            add(string.format('<circle cx="%s" cy="%s" r="3.5" fill="%s"/>', x, y, color))
          end
        end
      end
    end
  else
    -- Bars: grouped, or stacked.
    local groups = stacked and 1 or #data.series
    for i = 1, n do
      local center, band = category_center(i)
      local inner = band * 0.75
      local barw = inner / groups
      local pos_acc, neg_acc = 0, 0
      for si, s in ipairs(data.series) do
        local v = s.values[i]
        if v then
          local color = COLORS[(si - 1) % #COLORS + 1]
          local a, b
          if stacked then
            if v >= 0 then a, b = pos_acc, pos_acc + v pos_acc = b else a, b = neg_acc, neg_acc + v neg_acc = b end
          else
            a, b = 0, v
          end
          local p1, p2 = vpos(math.max(a, vmin)), vpos(math.min(math.max(b, vmin), vmax))
          local offset = stacked and 0 or (si - 1) * barw
          local start = center - inner / 2 + offset
          if horizontal then
            add(string.format('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>', math.min(p1, p2), start,
              math.abs(p2 - p1), math.max(0.5, barw - 1), color))
          else
            add(string.format('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>', start, math.min(p1, p2),
              math.max(0.5, barw - 1), math.abs(p2 - p1), color))
          end
        end
      end
    end
  end

  -- Category axis and labels.
  if zero then
    if horizontal then
      add(string.format('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#6b7280"/>', zero, plot.y, zero, plot.y + plot.h))
    elseif kind ~= "scatter" then
      add(string.format('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#6b7280"/>', plot.x, zero, plot.x + plot.w, zero))
    end
  end
  if kind ~= "scatter" then
    local every = 1
    if not horizontal and n > 24 then every = math.ceil(n / 24) end
    if horizontal and n > 40 then every = math.ceil(n / 40) end
    for i = 1, n, every do
      local c = category_center(i)
      local label = shorten(data.labels[i], 24)
      if horizontal then
        add(text(plot.x - 6, c + 4, label, { anchor = "end", size = 11 }))
      elseif rotate then
        add(text(c, plot.y + plot.h + 14, label, { anchor = "end", size = 11, rotate = -40 }))
      else
        add(text(c, plot.y + plot.h + 16, label, { anchor = "middle", size = 11 }))
      end
    end
  end
  if attrs["xlabel"] then
    add(text(plot.x + plot.w / 2, H - 6, attrs["xlabel"], { anchor = "middle", size = 12, color = "#555555" }))
  end
  if attrs["ylabel"] and not horizontal then
    add(text(14, plot.y + plot.h / 2, attrs["ylabel"], { anchor = "middle", size = 12, color = "#555555", rotate = -90 }))
  end
  if legend then
    local lx = plot.x
    local ly = (title and title ~= "" and 38 or 10)
    for si, s in ipairs(data.series) do
      local color = COLORS[(si - 1) % #COLORS + 1]
      add(string.format('<rect x="%.1f" y="%.1f" width="12" height="12" fill="%s"/>', lx, ly, color))
      local label = shorten(s.name, 26)
      add(text(lx + 16, ly + 10, label, { size = 12 }))
      lx = lx + 30 + 7 * (utf8.len(label) or #label)
    end
  end
  return out, W, H
end

local KINDS = { bar = true, column = true, hbar = true, stacked = true, line = true, area = true, scatter = true,
                pie = true }

local function make_chart(div, rows)
  local attrs = div.attributes
  local kind = attrs["type"] or "bar"
  if kind == "column" then kind = "bar" end
  if not KINDS[kind] then return nil, "Charts are bar, hbar, stacked, line, area, scatter or pie, not " .. kind .. "." end
  local data, err = chart_data(div, rows)
  if not data then return nil, err end
  local parts, W, H = svg_chart(kind, data, attrs)
  if not parts then return nil, W end
  local svg = string.format('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
    .. 'font-family="%s"><rect width="%d" height="%d" fill="#ffffff"/>%s</svg>', W, H, W, H, FONT, W, H,
    table.concat(parts))
  local alt = attrs["alt"] or stringify(div.content)
  if alt == "" then alt = "Chart" end
  local inline = G.svg_inline(svg, { width = attrs["width"] or "80%", alt = alt })
  local caption = {}
  if #div.content > 0 and div.content[1].content then caption = div.content[1].content end
  return G.block(inline, caption, div.identifier, { "chart" })
end

function Div(div)
  local is_table, is_chart = has_class(div, "data"), has_class(div, "chart")
  if not is_table and not is_chart then return nil end
  local src = div.attributes["src"]
  if not src then return nil end
  if G.keeps_source() then return nil end
  local path = safe_path(src)
  if not path then return G.problem(is_chart and "Chart" or "Data table", src .. " isn't a file in this project.") end
  local rows, err = load(path)
  if not rows then return G.problem(is_chart and "Chart" or "Data table", err) end
  local result
  if is_chart then
    result, err = make_chart(div, rows)
  else
    result, err = make_table(div, rows)
  end
  if not result then return G.problem(is_chart and "Chart" or "Data table", err) end
  return result
end

return {
  { Meta = function(meta) G.read_meta(meta) end },
  { Div = Div },
}
