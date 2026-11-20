--[[
dwgraphics.lua: pictures the filters make themselves (ARCHITECTURE.md §9.7), in every
output format. diagram.lua (Graphviz, Mermaid flowcharts, Typst drawings) and data.lua
(charts from .csv and .tsv files) make SVG; this module puts it into the document:

- Typst: the SVG as bytes in the .typ file, scaled down to the text width when wider,
  so nothing has to be written next to it.
- HTML and EPUB: an image with a data: address (the preview's and published pages' CSP
  allow data: images; inline SVGs would share element ids and styles).
- Word, OpenDocument and PowerPoint: the SVG in pandoc's media bag, embedded by the writer.
- LaTeX: a PDF, made by Typst from the SVG (TeX can't read SVG), in _dw-media/ next to the
  document.
- Markdown outputs keep the source as written; callers check `M.keeps_source()`.

Typst runs from here only with --root at a temporary folder of its own and without system
fonts, as the worker runs it (§9.6). Font folders come from the job's metadata (dw-font-paths).
]]

local M = {}
local stringify = pandoc.utils.stringify

M.font_paths = {}

function M.read_meta(meta)
  local paths = meta["dw-font-paths"]
  if type(paths) == "table" then
    M.font_paths = {}
    for _, p in ipairs(paths) do M.font_paths[#M.font_paths + 1] = stringify(p) end
  end
end

function M.target()
  if FORMAT == "typst" then return "typst" end
  if FORMAT:match("latex") or FORMAT == "beamer" then return "latex" end
  if FORMAT:match("html") or FORMAT:match("^epub") or FORMAT == "revealjs" then return "html" end
  if FORMAT == "docx" or FORMAT == "odt" or FORMAT == "opendocument" or FORMAT == "pptx" then return "office" end
  return "text"
end

function M.keeps_source()
  return M.target() == "text"
end

function M.warn(message)
  if pandoc.log and pandoc.log.warn then
    pandoc.log.warn(message)
  else
    io.stderr:write("[WARNING] " .. message .. "\n")
  end
end

local function typst_string(s)
  return '"' .. s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\r", ""):gsub("\n", "\\n"):gsub("\t", "\\t") .. '"'
end
M.typst_string = typst_string

-- An address for an <img>: the SVG percent-encoded, which keeps it readable and needs no base64.
function M.data_url(svg, mime)
  local enc = svg:gsub("[^%w%-%._~ /=:;,'()!*@+?&]", function(c)
    return string.format("%%%02X", c:byte())
  end):gsub(" ", "%%20")
  return "data:" .. (mime or "image/svg+xml") .. "," .. enc
end

-- Runs Typst on *source* in a folder of its own; returns the output as bytes, or nil and the error.
function M.typst(source, fmt, files, extra)
  local ok, result = pcall(function()
    return pandoc.system.with_temporary_directory("dw-typst", function(dir)
      local input = pandoc.path.join({ dir, "graphic.typ" })
      local out = pandoc.path.join({ dir, "graphic." .. fmt })
      for name, data in pairs(files or {}) do
        local f = assert(io.open(pandoc.path.join({ dir, name }), "wb"))
        f:write(data)
        f:close()
      end
      local f = assert(io.open(input, "wb"))
      f:write(source)
      f:close()
      local args = { "compile", "--root", dir, "--ignore-system-fonts", "--format", fmt }
      for _, a in ipairs(extra or {}) do args[#args + 1] = a end
      for _, p in ipairs(M.font_paths) do
        args[#args + 1] = "--font-path"
        args[#args + 1] = p
      end
      args[#args + 1] = input
      args[#args + 1] = out
      pandoc.pipe("typst", args, "")
      local r = assert(io.open(out, "rb"))
      local data = r:read("a")
      r:close()
      return data
    end)
  end)
  if ok then return result end
  return nil, M.error_text(result)
end

-- The useful part of a failed command's message.
function M.error_text(err)
  local s = type(err) == "table" and (err.output or err.message) or tostring(err)
  if type(err) == "table" and err.output == "" then s = tostring(err.message or err) end
  s = tostring(s):gsub("\27%[[%d;]*m", "")
  return (s:gsub("^%s+", ""):gsub("%s+$", ""):sub(1, 600))
end

-- An SVG as a PDF for LaTeX, through Typst.
function M.svg_to_pdf(svg)
  local src = '#set page(width: auto, height: auto, margin: 0pt)\n#image("graphic.svg")\n'
  return M.typst(src, "pdf", { ["graphic.svg"] = svg })
end

local function write_media(name, data)
  pandoc.system.make_directory("_dw-media", true)
  local path = "_dw-media/" .. name
  local f = assert(io.open(path, "wb"))
  f:write(data)
  f:close()
  return path
end

--[[
The picture as an inline element for the current format. opts:
  width   "80%" or "12cm"; empty: the picture's own size, at most the text width
  alt     text for screen readers
  pdf     bytes of a PDF of the same picture (LaTeX), else made from the SVG
]]
function M.svg_inline(svg, opts)
  opts = opts or {}
  local width = opts.width ~= "" and opts.width or nil
  local target = M.target()
  local hash = pandoc.utils.sha1(svg)
  if target == "typst" then
    local data = typst_string(svg)
    local code
    if width then
      code = string.format('#box(image(bytes(%s), format: "svg", width: %s))', data, width)
    else
      code = string.format('#box(layout(size => { let data = bytes(%s); let img = image(data, format: "svg"); '
        .. 'if measure(img).width > size.width { image(data, format: "svg", width: size.width) } else { img } }))',
        data)
    end
    return pandoc.RawInline("typst", code)
  end
  local attr = pandoc.Attr("", { "dw-graphic" }, width and { { "width", width } } or {})
  local alt = pandoc.Inlines(opts.alt and { pandoc.Str(opts.alt) } or {})
  if target == "html" then
    return pandoc.Image(alt, M.data_url(svg), "", attr)
  end
  if target == "latex" then
    local pdf, err = opts.pdf, nil
    if not pdf then pdf, err = M.svg_to_pdf(svg) end
    if not pdf then
      M.warn("A picture couldn't be converted for LaTeX: " .. (err or ""))
      return pandoc.Str("[" .. (opts.alt or "picture") .. "]")
    end
    return pandoc.Image(alt, write_media(hash .. ".pdf", pdf), "", attr)
  end
  -- Word, OpenDocument and PowerPoint: a PNG, which every version shows (an SVG needs a PNG
  -- fallback for older Word, which pandoc can only make with rsvg-convert).
  local png, err = M.svg_to_png(svg)
  if not png then
    M.warn("A picture couldn't be converted: " .. (err or ""))
    local name = hash .. ".svg"
    pandoc.mediabag.insert(name, "image/svg+xml", svg)
    return pandoc.Image(alt, name, "", attr)
  end
  if not width then
    local w = M.svg_width_pt(svg)
    if w then attr.attributes["width"] = string.format("%.2fin", math.min(w / 72, 6.3)) end
  end
  local name = hash .. ".png"
  pandoc.mediabag.insert(name, "image/png", png)
  return pandoc.Image(alt, name, "", attr)
end

-- The width an SVG asks for, in points (Graphviz writes pt, Typst pt, data.lua px).
function M.svg_width_pt(svg)
  local head = svg:match("<svg[^>]*>")
  if not head then return nil end
  local n, unit = head:match('%swidth="([%d%.]+)(%a*)"')
  n = tonumber(n)
  if not n then return nil end
  if unit == "" or unit == "px" then return n * 0.75 end
  if unit == "pt" then return n end
  if unit == "in" then return n * 72 end
  if unit == "cm" then return n * 72 / 2.54 end
  if unit == "mm" then return n * 72 / 25.4 end
  return nil
end

function M.svg_to_png(svg)
  local src = '#set page(width: auto, height: auto, margin: 0pt)\n#image("graphic.svg")\n'
  return M.typst(src, "png", { ["graphic.svg"] = svg }, { "--ppi", "200" })
end

--[[
A block for the picture: a numbered figure when it has an identifier or a caption (crossref.lua
numbers fig: identifiers), else the picture centred on its own line.
]]
function M.block(inline, caption, id, classes)
  caption = caption or {}
  if (id and id ~= "") or #caption > 0 then
    local blocks = pandoc.Blocks({ pandoc.Plain({ inline }) })
    local long = #caption > 0 and pandoc.Blocks({ pandoc.Plain(caption) }) or pandoc.Blocks({})
    return pandoc.Figure(blocks, { long = long }, pandoc.Attr(id or "", classes or {}))
  end
  if M.target() == "typst" then
    return pandoc.Plain({ pandoc.RawInline("typst", "#align(center)["), inline, pandoc.RawInline("typst", "]") })
  end
  return pandoc.Div({ pandoc.Plain({ inline }) }, pandoc.Attr("", { "dw-graphic-block", table.unpack(classes or {}) }))
end

-- A short, visible note where a picture couldn't be made, plus a warning for the editor.
function M.problem(what, message)
  M.warn(what .. ": " .. message)
  local text = pandoc.Inlines({ pandoc.Strong({ pandoc.Str(what) }), pandoc.Str(":"), pandoc.Space() })
  text:extend(pandoc.Inlines((message:gsub("\n.*", ""))))
  return pandoc.Div({ pandoc.Para(text) }, pandoc.Attr("", { "dw-graphic-error" }))
end

-- The caption from a caption="…" attribute, read as Markdown.
function M.caption_from(attr)
  local text = attr.attributes["caption"]
  if not text or text == "" then return {} end
  local doc = pandoc.read(text, "markdown")
  if doc.blocks[1] and doc.blocks[1].content then return doc.blocks[1].content end
  return {}
end

return M
