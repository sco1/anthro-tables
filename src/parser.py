import re
import typing as t
from collections import deque, namedtuple
from pathlib import Path

import more_itertools as miter
import pandas as pd

from src import converters

OUT_FILEPATH = Path("./converted_anthro_table.xlsx")

CONVERTER_MAP = {
    "WEIGHT": converters.weight,
    "WEIGTH-NUDE": converters.weight,  # Key typo intentional
    "WEIGTH NUDE": converters.weight,  # Key typo intentional
    "AGE": converters.age,
    "MOS": converters.mos,
    "RACE": converters.race,
    "BIRTH DATE": converters.birth_date,
    "LENGHT OF SERVICE": converters.length_of_service,  # Key typo intentional
    "RANK": converters.rank,
    "PLACE OF BIRTH": converters.birthplace,
    "HANDEDNESS": converters.handedness,
}


FieldSpec = namedtuple("FieldSpec", ["n_repeats", "type", "width"])


def extract_variable_names(full_text: list[str]) -> tuple[list[str], str, int]:
    """
    Extract the variable names from the provided data file.

    `full_text` is assumed to be a list of strings generated by `str.splitlines()` on the data file.
    The table is assumed to start at the beginning of the file and look something like the
    following:
        `   1  WEIGHT               86750  218000  132100  5000  3000   0453592  22046226`

    Each row corresponds to a data column; only the string is extracted, all other information is
    ignored.

    A `"SUBJECT ID"` varaible is prepended to the resulting list of names to account for the integer
    subject ID in the raw data table.

    Also returned is the data table format specifier, assumed to be on the line after the variable
    name enumeration concludes, and of the following form:
        ` (I4,19F4.0/20F4.0/20F4.0/9F4.0,F2.0,5F3.0,2F6.0,3F3.0)                         `

    NOTE: The format spec is stripped of leading/trailing whitespace, along with `()`

    The (0-indexed) row number of the first data row is also returned.
    """
    var_names = ["SUBJECT ID"]
    for _idx, line in enumerate(full_text):  # pragma: no branch
        # Check if we've gotten to the data format spec
        if line.strip().startswith("("):
            break

        # There should be at least 2 spaces between columns & we only care about column 2
        # Leading whitespace is not significant here
        split_line = re.split(r"\s{2,}", line.strip(), maxsplit=2)
        var_names.append(split_line[1])

    format_spec = full_text[_idx].strip(" ()")
    data_start_idx = _idx + 1

    return var_names, format_spec, data_start_idx


def parse_format_spec(raw_spec: str) -> tuple[list[FieldSpec], int]:
    """
    Parse the provided format specifier into a list of its field specifications.

    The raw format specifier is assumed to be of the following form:
        <n repeats><type><width>.<decimal width>
    e.g.
        ` (I4,19F4.0/20F4.0/20F4.0/9F4.0,F2.0,5F3.0,2F6.0,3F3.0)                         `

    If <n repeats> isn't specified it's assumed to be 1.

    Specifiers are delimited either by a comma (change in type) or a forward slash (newline).

    Also returned is the number of lines actually taken up by one row of data in the data table. The
    data is restricted to a line length of 80 characters, so a row of data may end up taking up
    multiple lines. As the format is fixed, this chunk size is the same for all subjects in the file

    NOTE: All currently encountered data specifies 0 decimal digits so the decimal width specifier
    is ignored.
    """
    chunk_size = raw_spec.count("/") + 1

    # Format spec should be received without whitespace or parentheses, but do it again as a guard
    fields = re.split(r"[,/]", raw_spec.strip(" ()"))
    spec_pattern = r"(\d*)(\w)(\d+)\.?"
    matches = []
    for field in fields:
        match = re.search(spec_pattern, field)

        if not match:
            raise ValueError(f"Unknown field specifier: '{field}'")

        matches.append(match.groups())

    field_specs = []
    for n_repeats, field_type, field_width in matches:
        field_specs.append(
            FieldSpec(
                int(n_repeats) if n_repeats else 1,
                field_type,
                int(field_width),
            )
        )

    return field_specs, chunk_size


def do_inplace_conversions(
    parsed_df: pd.DataFrame, converter_mapping: dict[str, t.Callable] = CONVERTER_MAP
) -> pd.DataFrame:
    """
    Attempt to apply the specified converters to their mapped variable names.

    If the variable is not present in the provided DataFrame, the conversion is ignored.

    NOTE: Mapped variable names are case-sensitive
    """
    for var_name, converter in converter_mapping.items():
        try:
            parsed_df[var_name] = parsed_df[var_name].apply(converter)
        except KeyError:
            continue

    return parsed_df


def parse_data(full_text: list[str]) -> pd.DataFrame:
    """
    Helper pipeline to parse the provided data file into a Pandas DataFrame.

    `full_text` is assumed to be a list of strings generated by `str.splitlines()` on the data file.
    The table is assumed to start at the beginning of the file and look something like the
    following:
        ```
            1  WEIGHT               86750  218000  132100  5000  3000   0453592  22046226
            <... variables>
             (I4,19F4.0/20F4.0/20F4.0/9F4.0,F2.0,5F3.0,2F6.0,3F3.0)
            <... data>

    The resulting DataFrame uses the parsed variable names as column headers and is indexed by
    subject ID.
    """
    var_names, format_spec, data_start_idx = extract_variable_names(full_text)
    field_specs, chunk_size = parse_format_spec(format_spec)

    parsed_rows = []
    for chunk in miter.chunked(full_text[data_start_idx:], chunk_size):
        parsed_row = []

        # Trailing whitespace is just padding to get to 80 characters for the line
        # Dump row into a deque so we can pop off the leading digits as we read
        row = deque("".join(chunk).rstrip())
        for field in field_specs:
            for _ in range(field.n_repeats):
                parsed_row.append(int("".join(row.popleft() for _ in range(field.width))))

        parsed_rows.append(parsed_row)

    parsed_df = pd.DataFrame(parsed_rows, columns=var_names).set_index("SUBJECT ID")

    return parsed_df


def batch_parse(
    file_list: dict[str, Path], out_filepath: Path = OUT_FILEPATH, inplace_decoding: bool = True
) -> None:  # pragma: no cover
    """
    Batch parse the provided files into an Excel spreadsheet.

    `file_list` is assumed to be a dictionary mapping the desired sheet name to its corresponding
    data file.

    The `inplace_decoding` flag can be set to decode complex fields (e.g. MOS, Age, Rank) in-place
    in the parsed dataframe. Decoding is done by default.
    """
    raw_dfs = {}
    for file_key, filepath in file_list.items():
        full_text = filepath.read_text().splitlines()

        if inplace_decoding:
            raw_dfs[file_key] = do_inplace_conversions(parse_data(full_text))
        else:
            raw_dfs[file_key] = parse_data(full_text)

    with pd.ExcelWriter(out_filepath) as writer:
        for file_id, df in raw_dfs.items():
            df.to_excel(writer, sheet_name=file_id)
