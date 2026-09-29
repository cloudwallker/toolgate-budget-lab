"""A fixed, read-only demonstration catalog; no network or filesystem writes."""

CATALOG = {
    "SKU-001": {"name": "机械键盘", "description": "有线键盘，适合日常办公。"},
    "SKU-002": {"name": "显示器", "description": "27英寸显示器，适合文档与代码阅读。"},
}


def execute(tool: str, sku: str) -> dict:
    record = CATALOG.get(sku)
    if record is None:
        return {"sku": sku, "found": False}
    if tool == "catalog.lookup":
        return {"sku": sku, "found": True, **record}
    if tool == "catalog.summary":
        return {"sku": sku, "summary": f"{record['name']}：{record['description']}"}
    raise ValueError("Unsupported tool")
