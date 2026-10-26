"""The AXPP preparation must preserve unrelated customizations and metadata."""

import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile


spec = importlib.util.spec_from_file_location(
    "patch_d365_full_name", Path(__file__).resolve().parents[2] / "scripts/patch_d365_full_name.py",
)
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


def metadata(kind, name, source, methods="", extra=""):
    return f'''<Ax{kind}><Name>{name}</Name><SourceCode>
        <Declaration><![CDATA[{source}]]></Declaration><Methods>{methods}</Methods>
        </SourceCode>{extra}</Ax{kind}>'''


def method(name, source):
    return f"<Method><Name>{name}</Name><Source><![CDATA[{source}]]></Source></Method>"


def fixture_objects():
    values = {
        "ProjectItem/AxClass/UBHRPortalService.xml": metadata("Class", "UBHRPortalService", "class UBHRPortalService {}",
            method("submitApplication", "old submission") + method("requireFieldText", "preserve custom size checks")),
        "ProjectItem/AxClass/UBHRPortalApplicationRequestContract.xml": metadata("Class", "UBHRPortalApplicationRequestContract",
            "class UBHRPortalApplicationRequestContract { str requirementId; str firstName; str lastName; }",
            method("parmFirstName", "legacy accessor")),
        "ProjectItem/AxTable/UBCandidateTable.xml": metadata("Table", "UBCandidateTable", "class UBCandidateTable {}",
            method("insert", "preserve number sequence"), "<TitleField2>FirstName</TitleField2>"),
        "ProjectItem/AxForm/UBCandidateForm.xml": '''<AxForm xmlns="Microsoft.Dynamics.AX.Metadata.V6" xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
            <DataSources><AxFormDataSource xmlns=""><Name>UBCandidateTable</Name><Fields>
            <AxFormDataSourceField><DataField>FirstName</DataField></AxFormDataSourceField>
            </Fields></AxFormDataSource></DataSources><Design><Controls>
            <AxFormControl xmlns="" i:type="AxFormStringControl"><Name>UBCandidateTable_FirstName</Name>
            <DataField>FirstName</DataField><DataSource>UBCandidateTable</DataSource></AxFormControl>
            <AxFormControl xmlns="" i:type="AxFormStringControl"><Name>UBCandidateTable_LastName</Name>
            <DataField>LastName</DataField><DataSource>UBCandidateTable</DataSource></AxFormControl>
            </Controls></Design></AxForm>''',
    }
    for table in ("UBJobApplicationTable", "UBCandidateScreeningTable"):
        values[f"ProjectItem/AxTable/{table}.xml"] = metadata("Table", table, f"class {table} {{}}",
            method("getCandidateName", "old name lookup") + method("insert", "preserve table insert"))
    return values


def test_name_patch_keeps_custom_validation_number_sequences_legacy_contract_and_form_namespaces():
    result = patch.patch_objects(fixture_objects())
    service = result["ProjectItem/AxClass/UBHRPortalService.xml"]
    assert "preserve custom size checks" in service
    root = ET.fromstring(service)
    submit = next(m.findtext("Source") for m in root.findall("./SourceCode/Methods/Method")
                  if m.findtext("Name") == "submitApplication")
    assert submit.index("fieldNum(UBJobApplicationTable, CandidateName)") < submit.index("application.insert();")
    assert "fieldNum(UBCandidateTable, CandidateName)" in submit
    contract = result["ProjectItem/AxClass/UBHRPortalApplicationRequestContract.xml"]
    assert 'DataMemberAttribute("CandidateName")' in contract and "legacy accessor" in contract
    assert "preserve number sequence" in result["ProjectItem/AxTable/UBCandidateTable.xml"]
    for table in ("UBJobApplicationTable", "UBCandidateScreeningTable"):
        text = result[f"ProjectItem/AxTable/{table}.xml"]
        assert "preserve table insert" in text
        assert text.index("return this.CandidateName") < text.index("select firstOnly candidate")
        assert "candidate.FirstName, candidate.LastName" in text
    form = ET.fromstring(result["ProjectItem/AxForm/UBCandidateForm.xml"])
    assert form.tag == "{Microsoft.Dynamics.AX.Metadata.V6}AxForm"
    fields = [n.text for n in form.iter() if n.tag == "DataField"]
    assert "CandidateName" in fields and "LastName" in fields and "FirstName" not in fields


def test_archive_preparation_keeps_all_other_entries_byte_for_byte(tmp_path):
    original, output = tmp_path / "original.axpp", tmp_path / "prepared.axpp"
    with zipfile.ZipFile(original, "w") as archive:
        for name, value in fixture_objects().items():
            archive.writestr(name.replace("/", "\\"), value)
        archive.writestr("ProjectItem\\AxClass\\CustomWorkflow.xml", b"<Custom>untouched</Custom>")
    before = original.read_bytes()
    report = patch.patch_archive(original, output)
    assert original.read_bytes() == before
    assert len(report["changed_existing_objects"]) == 6 and report["unchanged_objects"] == 1
    assert not report["xpp_compiled"] and not report["visual_studio_import_verified"]
    with zipfile.ZipFile(output) as archive:
        assert archive.read("ProjectItem\\AxClass\\CustomWorkflow.xml") == b"<Custom>untouched</Custom>"
