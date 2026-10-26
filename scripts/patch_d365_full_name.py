"""Patch existing AXPP objects for CandidateName without rewriting other objects.

This prepares source for F&O developer review/import. It is not an X++ compiler
or a deployable runtime package. The input AXPP is never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def source_method(source: str, name: str) -> str:
    match = re.search(
        r"(?ms)^    (?:\[[^\n]+\]\n    )*(?:public|private|protected)\s+"
        r"(?:static\s+)?\w+\s+" + re.escape(name) + r"\([^)]*\)\s*\n    \{.*?^    \}", source,
    )
    if not match:
        raise ValueError(f"Source method not found: {name}")
    return match.group()


def replace_method(xml: str, name: str, source: str) -> str:
    pattern = r"(?s)<Method>\s*<Name>" + re.escape(name) + r"</Name>.*?</Method>"
    matches = list(re.finditer(pattern, xml))
    if len(matches) > 1:
        raise ValueError(f"Duplicate method: {name}")
    if matches:
        block = matches[0].group()
        updated, count = re.subn(
            r"(?s)(<Source><!\[CDATA\[).*?(\]\]></Source>)",
            lambda m: m[1] + "\n" + source + "\n" + m[2], block,
        )
        if count != 1:
            raise ValueError(f"Expected one CDATA method source: {name}")
        return xml[:matches[0].start()] + updated + xml[matches[0].end():]
    if xml.count("</Methods>") != 1:
        raise ValueError("Expected one method collection")
    return xml.replace("</Methods>",
        f"<Method><Name>{name}</Name><Source><![CDATA[\n{source}\n]]></Source></Method>\n</Methods>")


DISPLAY_NAME_METHOD = '''    public display str getCandidateName()
    {
        UBCandidateTable candidate;

        // Prefer the name captured for this application/screening.
        if (strLen(strLRTrim(this.CandidateName)))
        {
            return this.CandidateName;
        }

        select firstOnly candidate
            where candidate.CandidateId == this.CandidateId;

        if (candidate.RecId)
        {
            if (strLen(strLRTrim(candidate.CandidateName)))
            {
                return candidate.CandidateName;
            }
            // Read compatibility for existing records; no name splitting.
            return strLRTrim(strFmt("%1 %2", candidate.FirstName, candidate.LastName));
        }
        return "";
    }'''


def patch_objects(objects: dict[str, str]) -> dict[str, str]:
    """Change only the six known existing name-related metadata objects."""
    updated = dict(objects)
    service = (ROOT / "d365/xpp/UBHRPortalService.xpp").read_text()
    contract = (ROOT / "d365/xpp/UBHRPortalApplicationRequestContract.xpp").read_text()
    name = "ProjectItem/AxClass/UBHRPortalService.xml"
    for method in ("submitApplication", "requestCandidateName"):
        updated[name] = replace_method(updated[name], method, source_method(service, method))
    name = "ProjectItem/AxClass/UBHRPortalApplicationRequestContract.xml"
    if "str candidateName;" not in updated[name]:
        updated[name], count = re.subn(r"str requirementId;", "str requirementId;\n    str candidateName;", updated[name], count=1)
        if count != 1:
            raise ValueError("Unexpected application contract declaration")
    updated[name] = replace_method(updated[name], "parmCandidateName", source_method(contract, "parmCandidateName"))
    for table in ("UBJobApplicationTable", "UBCandidateScreeningTable"):
        name = f"ProjectItem/AxTable/{table}.xml"
        updated[name] = replace_method(updated[name], "getCandidateName", DISPLAY_NAME_METHOD)
    name = "ProjectItem/AxTable/UBCandidateTable.xml"
    if "<TitleField2>FirstName</TitleField2>" not in updated[name] and "<TitleField2>CandidateName</TitleField2>" not in updated[name]:
        raise ValueError("Unexpected candidate title field; review manually")
    updated[name] = updated[name].replace("<TitleField2>FirstName</TitleField2>", "<TitleField2>CandidateName</TitleField2>")
    name = "ProjectItem/AxForm/UBCandidateForm.xml"
    xml = updated[name]
    # Existing mixed-namespace XML/CDATA must stay intact: patch leaf controls,
    # not a reserialized form tree. Keep legacy LastName controls available.
    def control(match):
        block = match.group()
        if "<DataField>FirstName</DataField>" in block and "<DataSource>UBCandidateTable</DataSource>" in block:
            block = block.replace("<DataField>FirstName</DataField>", "<DataField>CandidateName</DataField>")
            block = re.sub(r"(<Name>[^<]*)FirstName", r"\1CandidateName", block)
        return block
    xml, _ = re.subn(r"(?s)<AxFormControl\b[^>]*>(?:(?!<AxFormControl\b).)*?</AxFormControl>", control, xml)
    if "<DataField>CandidateName</DataField>" not in xml:
        raise ValueError("Candidate form name controls were not found")
    if not re.search(r"<AxFormDataSourceField>\s*<DataField>CandidateName</DataField>", xml):
        xml = xml.replace("<DataField>FirstName</DataField>", "<DataField>CandidateName</DataField>", 1)
    updated[name] = xml
    for xml in updated.values():
        ET.fromstring(xml)
    return updated


def patch_archive(source: Path, target: Path):
    if source.resolve() == target.resolve():
        raise ValueError("Input and output must differ")
    names = [
        "ProjectItem/AxClass/UBHRPortalService.xml",
        "ProjectItem/AxClass/UBHRPortalApplicationRequestContract.xml",
        "ProjectItem/AxTable/UBJobApplicationTable.xml",
        "ProjectItem/AxTable/UBCandidateScreeningTable.xml",
        "ProjectItem/AxTable/UBCandidateTable.xml",
        "ProjectItem/AxForm/UBCandidateForm.xml",
    ]
    with zipfile.ZipFile(source) as original:
        if sum(i.file_size for i in original.infolist()) > 16 * 1024 * 1024:
            raise ValueError("Unexpectedly large project; review archive manually")
        by_name = {i.filename.replace("\\", "/"): i.filename for i in original.infolist()}
        objects = {n: original.read(by_name[n]).decode("utf-8-sig").replace("\r\n", "\n") for n in names}
        updated = patch_objects(objects)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for info in original.infolist():
                key = info.filename.replace("\\", "/")
                value = updated[key].replace("\n", "\r\n").encode() if key in updated else original.read(info)
                output.writestr(info, value)
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(target) as after:
        assert before.namelist() == after.namelist()
        assert after.testzip() is None
        changed = [n for n in before.namelist() if before.read(n) != after.read(n)]
        assert {n.replace("\\", "/") for n in changed} == set(names)
    record = {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "changed_existing_objects": changed,
        "unchanged_objects": len(by_name) - len(changed),
        "xpp_compiled": False,
        "visual_studio_import_verified": False,
        "deployment": "F&O-first: review/import/build existing objects, then deploy backend",
    }
    target.with_suffix(".verification.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    print(json.dumps(patch_archive(args.source, args.target), indent=2))
