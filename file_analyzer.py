from struct import *
from optparse import OptionParser


class WrongFileType(Exception):
    pass


class CertificateError(Exception):
    pass


class EXEAnalyzer:
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        try:
            assert self._validate_file()
        except AssertionError as e:
            raise WrongFileType("Given file is not a PE") from e
        self.filedata = self._collect_info()

    def _validate_file(self) -> bool:
        return open(self.filepath, "rb").read(2) == b"MZ"

    def _collect_info(self) -> dict:
        data = {}
        with open(self.filepath, "rb") as file:
            # starting in the DOS header
            file.seek(60)
            data["PE Header location"] = unpack('<i', file.read(4))[0]

            # moving into the COFF header
            file.seek(data["PE Header location"])
            data["Signature 0x50450000"] = file.read(4)
            data["COFF start"] = file.tell()
            file.seek(data["COFF start"])
            data["Machine"] = unpack('<H', file.read(2))[0]
            data["#NumberOfSections"] = unpack('<H', file.read(2))[0]
            data["TimeDateStamp"] = unpack('<I', file.read(4))[0]
            data["PointerToSymbolTable"] = unpack('<I', file.read(4))[0]
            data["#NumberOfSymbolTable"] = unpack('<I', file.read(4))[0]
            data["SizeOfOptionalHeader"] = unpack('<H', file.read(2))[0]
            data["Characteristics (COFF Header)"] = unpack('<H', file.read(2))[0]

            # Entering Standard COFF Fields
            data["Magic"] = unpack('<H', file.read(2))[0]
            pat, size = ('<Q', 8) if data['Magic'] == 0x20B else ('<I', 4)
            data["MajorLinkerVersion"] = unpack('!B', file.read(1))[0]
            data["MajorLinkerLinker"] = unpack('!B', file.read(1))[0]
            data["SizeOfCode"] = unpack('<I', file.read(4))[0]
            data["SizeOfInitializedData"] = unpack('<I', file.read(4))[0]
            data["AddressOfEntryPoint (RVA)"] = unpack('<I', file.read(4))[0]
            data["BaseOfCode (RVA)"] = unpack('<I', file.read(4))[0]
            data["BaseOfData (RVA)"] = unpack('<I', file.read(4))[0]

            # Entering Windows Specific Fields
            data["ImageBase"] = unpack(pat, file.read(size))[0]
            data["SectionAlignment"] = unpack('<I', file.read(4))[0]
            data["FileAlignment"] = unpack('<I', file.read(4))[0]
            data["MajorOperatingSystemVersion"] = unpack('<H', file.read(2))[0]
            data["MinorOperatingSystemVersion"] = unpack('<H', file.read(2))[0]
            data["MajorImageVersion"] = unpack('<H', file.read(2))[0]
            data["MinorImageVersion"] = unpack('<H', file.read(2))[0]
            data["MajorSubsystemVersion"] = unpack('<H', file.read(2))[0]
            data["MinorSubsystemVersion"] = unpack('<H', file.read(2))[0]
            data["Win32VersionValue"] = unpack('<I', file.read(4))[0]
            data["SizeOfImage"] = unpack('<I', file.read(4))[0]
            data["SizeOfHeaders"] = unpack('<I', file.read(4))[0]
            data["CheckSum"] = unpack('<I', file.read(4))[0]
            data["Subsystem"] = unpack('<H', file.read(2))[0]
            data["DllCharacteristics"] = unpack('<H', file.read(2))[0]
            data['SizeOfStackReserve'] = unpack(pat, file.read(8))[0]
            data['SizeOfStackCommit'] = unpack(pat, file.read(size))[0]
            data['SizeOfHeapReserve'] = unpack(pat, file.read(size))[0]
            data['SizeOfHeapCommit'] = unpack(pat, file.read(size))[0]
            data["LoaderFlags"] = unpack('<I', file.read(4))[0]
            data["#NumberOfRvaAndSizes"] = unpack('<I', file.read(4))[0]

            # Entering the Optional header
            data["ExportTable (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfExportTable"] = unpack('<I', file.read(4))[0]

            data["ImportTable (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfImportTable"] = unpack('<I', file.read(4))[0]

            data["ResourceTable (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfResourceTable"] = unpack('<I', file.read(4))[0]

            data["ExceptionTable (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfExceptionTable"] = unpack('<I', file.read(4))[0]

            data["CertificateTable Pointer location"] = file.tell()
            data["CertificateTable (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfCertificateTable Pointer location"] = file.tell()
            data["SizeOfCertificateTable"] = unpack('<I', file.read(4))[0]

            data["BaseRelocationTable (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfBaseRelocationTable"] = unpack('<I', file.read(4))[0]

            data["Debug (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfDebug"] = unpack('<I', file.read(4))[0]

            data["ArchitectData (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfArchitectData"] = unpack('<I', file.read(4))[0]

            data["GlobePtr(RVA)"] = unpack('<I', file.read(4))[0]
            file.seek(file.tell() + 4)

            data["TLSTable (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfTLSTable"] = unpack('<I', file.read(4))[0]

            data["LoadConfigTable (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfLoadConfigTable"] = unpack('<I', file.read(4))[0]

            data["BoundImport (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfBoundImport"] = unpack('<I', file.read(4))[0]

            data["ImportAddressTable (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfImportAddressTable"] = unpack('<I', file.read(4))[0]

            data["DelayImportDescriptor (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfDelayImportDescriptor"] = unpack('<I', file.read(4))[0]

            data["CLRRuntimeHeader (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfCLRRuntimeHeader"] = unpack('<I', file.read(4))[0]

            file.seek(file.tell() + 8)

            # Entering the Section Table
            data["Name"] = file.read(8)
            data["VirtualSize"] = unpack('<I', file.read(4))[0]
            data["VirtualAddress (RVA)"] = unpack('<I', file.read(4))[0]
            data["SizeOfRawData"] = unpack('<I', file.read(4))[0]
            data["PointerToRawData"] = unpack('<I', file.read(4))[0]
            data["PointerToRelocations"] = unpack('<I', file.read(4))[0]
            data["PointerToLinenumbers"] = unpack('<I', file.read(4))[0]
            data["NumberOfRelocations"] = unpack('<H', file.read(2))[0]
            data["NumberOfLinenumbers"] = unpack('<H', file.read(2))[0]
            data["Characteristics (Section Table)"] = unpack('<I', file.read(4))[0]
        return data

    def is_signed(self) -> bool:
        return bool(self.filedata["SizeOfCertificateTable"])

    def get_certificate(self) -> bytes:
        if not self.is_signed():
            raise CertificateError("Source file is not signed")
        with open(self.filepath, "rb") as file:
            file.seek(self.filedata["CertificateTable (RVA)"])
            certificate = file.read(self.filedata["SizeOfCertificateTable"])
        return certificate

    def write_certificate(self, outputfile: str) -> None:
        input_analyzer = EXEAnalyzer(outputfile)
        try:
            assert not input_analyzer.is_signed()
        except AssertionError as e:
            raise CertificateError("Input file already signed") from e
        input_data = input_analyzer.filedata
        certificate = self.get_certificate()
        size = pack('<I', self.filedata["SizeOfCertificateTable"])
        table_index = input_data["CertificateTable Pointer location"]
        sizeof_index = input_data["SizeOfCertificateTable Pointer location"]
        with open(outputfile, 'rb') as file:
            data = bytearray(file.read())
        data[table_index:sizeof_index] = pack('<I', len(data))
        data[sizeof_index:sizeof_index + len(size)] = size
        data += certificate
        with open(outputfile, 'wb') as file:
            file.write(data)

    def remove_certificate(self) -> None:
        if not self.is_signed():
            raise CertificateError("Source file is not signed")
        with open(self.filepath, "rb") as file:
            data = bytearray(file.read())
        loc = self.filedata["CertificateTable (RVA)"]
        size = self.filedata["SizeOfCertificateTable"]
        loc_index = self.filedata["CertificateTable Pointer location"]
        size_index = self.filedata["SizeOfCertificateTable Pointer location"]
        data[loc_index:loc_index + 4] = pack("<I", 0)
        data[size_index:size_index + 4] = pack("<I", 0)
        data = data[:loc] + data[loc + size + 1:]
        with open(self.filepath, 'wb') as file:
            file.write(data)
        # update the file data
        self.filedata["CertificateTable (RVA)"] = 0
        self.filedata["SizeOfCertificateTable"] = 0

    def __len__(self) -> int:
        return len(open(self.filepath, 'rb').read())


def main() -> None:
    parser = OptionParser()
    parser.add_option('-o', dest='outputfile')
    parser.add_option('-s', dest='sigfile')
    parser.add_option('--rm', dest='inputfile')
    (options, args) = parser.parse_args()

    outfile, sigfile, infile = options.outputfile, options.sigfile, options.inputfile
    if outfile and sigfile:
        f = EXEAnalyzer(sigfile)
        f.write_certificate(outfile)
        print(f"{outfile}: Signed.")
    elif infile:
        f = EXEAnalyzer(infile)
        f.remove_certificate()
        print(f"Signature removed from: {infile}.")
    else:
        print("Invalid arguments given.")


if __name__ == "__main__":
    main()
