"""Pure table shaping: rows of cell text -> list of dicts keyed by column name.

No driver, no IO. The driver's get_table_rows() output goes in; Excel-ready rows come out.

Rules (each one is pinned by a test):
- The table is as wide as its widest row (header included).
- Header names are stripped; blank or missing ones become positional `col_<index>`.
- Duplicate names are de-duplicated: `name`, `name_2`, `name_3`.
- Short rows are padded with None; cell values are otherwise left untouched.
"""

Row = dict[str, str | None]


def _dedupe(names: list[str]) -> list[str]:
    used: set[str] = set()
    result: list[str] = []
    for name in names:
        candidate = name
        counter = 2
        while candidate in used:
            candidate = f"{name}_{counter}"
            counter += 1
        used.add(candidate)
        result.append(candidate)
    return result


def _column_names(header: list[str], width: int) -> list[str]:
    names = []
    for index in range(width):
        raw = header[index].strip() if index < len(header) else ""
        names.append(raw or f"col_{index}")
    return _dedupe(names)


def extract_table(raw_rows: list[list[str]], has_header: bool = True) -> list[Row]:
    """Turn raw table rows into a list of dicts, one per data row."""
    if not raw_rows:
        return []

    header = raw_rows[0] if has_header else []
    data_rows = raw_rows[1:] if has_header else raw_rows

    width = max(len(row) for row in raw_rows)
    names = _column_names(header, width)

    return [
        {name: (row[index] if index < len(row) else None) for index, name in enumerate(names)}
        for row in data_rows
    ]
