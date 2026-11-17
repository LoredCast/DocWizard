--[[
pagebreak.lua: one page-break syntax for every output format (ARCHITECTURE.md §9.7).
A paragraph containing only \newpage or \pagebreak becomes a page break.
]]

local breaks = {
  latex = pandoc.RawBlock("latex", "\\newpage{}"),
  typst = pandoc.RawBlock("typst", "#pagebreak()"),
  openxml = pandoc.RawBlock("openxml", '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'),
  odt = pandoc.RawBlock("opendocument", '<text:p text:style-name="Pagebreak"/>'),
  html = pandoc.RawBlock("html", '<div style="break-after: page"></div>'),
}

local function target()
  if FORMAT:match("latex") or FORMAT == "beamer" then return "latex" end
  if FORMAT == "typst" then return "typst" end
  if FORMAT == "docx" then return "openxml" end
  if FORMAT == "odt" or FORMAT == "opendocument" then return "odt" end
  if FORMAT:match("html") or FORMAT:match("^epub") then return "html" end
  return nil
end

local function is_break(el)
  if el.t == "RawBlock" and (el.format == "tex" or el.format == "latex") then
    return el.text:match("^%s*\\newpage%s*$") or el.text:match("^%s*\\pagebreak%s*$")
  end
  if el.t == "Para" and #el.content == 1 and el.content[1].t == "RawInline" then
    local t = el.content[1].text
    return t:match("^\\newpage$") or t:match("^\\pagebreak$")
  end
  return false
end

return {
  {
    RawBlock = function(el) if is_break(el) then return breaks[target()] or {} end end,
    Para = function(el) if is_break(el) then return breaks[target()] or {} end end,
  },
}
