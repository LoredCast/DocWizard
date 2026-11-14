-- zotero.lua: `bibliography: zotero:<name>` entries name a configured Zotero source
-- (ARCHITECTURE.md §9.4). DocWizard writes each source's CSL-JSON to
-- _zotero/<name>.json in the job before pandoc runs; this filter points the
-- bibliography there. It runs before citeproc.

local function resolve(value)
  local text = pandoc.utils.stringify(value)
  local name = text:match("^zotero:([%w_%-]+)$")
  if name then
    return pandoc.MetaString("_zotero/" .. name .. ".json")
  end
  return value
end

function Meta(meta)
  local bib = meta.bibliography
  if bib == nil then
    return nil
  end
  if pandoc.utils.type(bib) == "List" then
    local out = pandoc.List()
    for _, entry in ipairs(bib) do
      out:insert(resolve(entry))
    end
    meta.bibliography = out
  else
    meta.bibliography = resolve(bib)
  end
  return meta
end
