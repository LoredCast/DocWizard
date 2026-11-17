--[[
dwlocale.lua: DocWizard's document languages (ARCHITECTURE.md §9.8), shared by lang.lua,
quotes.lua and crossref.lua. The data lives in locales.json next to this file.

  local L = require "dwlocale"
  L.normalize("de_at")          --> "de-AT"
  local loc = L.resolve("de-AT") --> merged entry: de, then de-AT on top
  L.format_date("2026-01-05", "long", loc) --> "5. Jänner 2026"
]]

local M = {}

local here = debug.getinfo(1, "S").source:gsub("^@", ""):match("^(.*)[/\\]") or "."
local data
do
  local f = io.open(here .. "/locales.json", "r")
  if f then
    data = pandoc.json.decode(f:read("a"), false)
    f:close()
  end
  data = data or { fallback = "en", languages = {}, names = {} }
end
M.data = data

-- "de_at" -> "de-AT", "German" -> "de", "zh-hant-tw" -> "zh-Hant-TW". nil if it isn't a language tag.
function M.normalize(tag)
  if type(tag) ~= "string" then return nil end
  tag = tag:gsub("^%s+", ""):gsub("%s+$", "")
  if tag == "" then return nil end
  local named = data.names[pandoc.text.lower(tag)]
  if named then return named end
  tag = tag:gsub("_", "-")
  local parts = {}
  for part in tag:gmatch("[^-]+") do parts[#parts + 1] = part end
  local lang = parts[1] and parts[1]:lower()
  if not lang or not lang:match("^%a%a%a?$") then return nil end
  local out = { lang }
  for i = 2, #parts do
    local p = parts[i]
    if p:match("^%a%a%a%a$") and #out == 1 then
      out[#out + 1] = p:sub(1, 1):upper() .. p:sub(2):lower()
    elseif p:match("^%a%a$") or p:match("^%d%d%d$") then
      out[#out + 1] = p:upper()
    elseif p:match("^%w%w%w%w%w?%w?%w?%w?$") then
      out[#out + 1] = p:lower() -- variants such as 1996
    else
      return nil
    end
  end
  return table.concat(out, "-")
end

local function merge(into, from)
  for k, v in pairs(from) do
    if type(v) == "table" and type(into[k]) == "table" and #v == 0 then
      local copy = {}
      for k2, v2 in pairs(into[k]) do copy[k2] = v2 end
      for k2, v2 in pairs(v) do copy[k2] = v2 end
      into[k] = copy
    else
      into[k] = v
    end
  end
end

-- The entry for a tag: the fallback language, then the base language, then the tag itself.
-- `known` says whether DocWizard has terms for the language; `quotes` whether it knows its marks.
function M.resolve(tag)
  tag = M.normalize(tag) or data.fallback
  local base = tag:match("^(%a+)")
  local region = tag:match("%-(%u%u)$") or tag:match("%-(%d%d%d)$")
  local out = { tag = tag, lang = base, region = region }
  merge(out, data.languages[data.fallback] or {})
  out.known, out.has_quotes = false, false
  local entry = data.languages[base]
  if entry then
    merge(out, entry)
    out.known = entry.terms ~= nil
    out.has_quotes = entry.quotes ~= nil
  end
  local full = region and data.languages[base .. "-" .. region]
  if full then
    merge(out, full)
    out.has_quotes = out.has_quotes or full.quotes ~= nil
  end
  out.tag, out.lang, out.region = tag, base, region
  return out
end

function M.term(loc, key)
  if loc.known then return loc.terms[key] end
  local fallback = data.languages[data.fallback]
  return fallback and fallback.terms[key]
end

local function pad(n) return string.format("%02d", n) end

-- Formats an ISO date (2026-11-16, 2026-11 or 2026) in one of the locale's formats. nil when
-- the text isn't such a date or the language has no date formats, so the date stays as written.
function M.format_date(text, fmt, loc)
  if fmt == "as-written" or not loc.date or not loc.months then return nil end
  text = text:gsub("^%s+", ""):gsub("%s+$", "")
  local y, m, d = text:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)$")
  if not y then
    y, m = text:match("^(%d%d%d%d)%-(%d%d)$")
  end
  if not y then return nil end
  y, m, d = tonumber(y), tonumber(m), d and tonumber(d)
  if m < 1 or m > 12 or (d and (d < 1 or d > 31)) then return nil end
  if fmt == "iso" then return text end
  local pattern
  if not d then
    pattern = loc.date["month-year"]
  else
    pattern = loc.date[fmt] or loc.date.long
  end
  if not pattern then return nil end
  local day = d and tostring(d) or ""
  if d == 1 and loc.date["first-day"] and pattern:find("{month}", 1, true) then day = loc.date["first-day"] end
  local values = {
    yyyy = tostring(y), month = loc.months[m], m = tostring(m), mm = pad(m),
    d = day, dd = d and pad(d) or "",
  }
  return (pattern:gsub("{(%a+)}", function(k) return values[k] or "" end))
end

-- 80000 -> "80,000" (en), "80.000" (de), "80 000" (fr)
function M.number(n, loc)
  local s = tostring(math.floor(n))
  local sep = loc.group or ","
  local head = #s % 3
  local parts = {}
  if head > 0 then parts[1] = s:sub(1, head) end
  for i = head + 1, #s, 3 do parts[#parts + 1] = s:sub(i, i + 2) end
  return table.concat(parts, sep)
end

return M
