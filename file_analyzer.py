from struct import pack, unpack
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

    # ── internal ─────────────────────────────────────────────────────────────

    def _validate_file(self) -> bool:
        with open(self.filepath, "rb") as f:
            return f.read(2) == b"MZ"

    def _collect_info(self) -> dict:
        data = {}
        with open(self.filepath, "rb") as file:
            # DOS header  →  offset to PE signature
            file.seek(60)
            data["PE Header location"] = unpack('<i', file.read(4))[0]

            # PE signature
            file.seek(data["PE Header location"])
            data["Signature 0x50450000"] = file.read(4)

            # COFF header  (20 bytes)
            data["COFF start"] = file.tell()
            data["Machine"]                   = unpack('<H', file.read(2))[0]
            data["#NumberOfSections"]          = unpack('<H', file.read(2))[0]
            data["TimeDateStamp"]              = unpack('<I', file.read(4))[0]
            data["PointerToSymbolTable"]       = unpack('<I', file.read(4))[0]
            data["#NumberOfSymbolTable"]       = unpack('<I', file.read(4))[0]
            data["SizeOfOptionalHeader"]       = unpack('<H', file.read(2))[0]
            data["Characteristics (COFF Header)"] = unpack('<H', file.read(2))[0]

            # Optional header  —  Standard COFF fields
            data["Magic"] = unpack('<H', file.read(2))[0]
            is_pe32plus   = data["Magic"] == 0x20B          # PE32+ has no BaseOfData
            pat, ptr_size = ('<Q', 8) if is_pe32plus else ('<I', 4)

            data["MajorLinkerVersion"]    = unpack('!B', file.read(1))[0]
            data["MajorLinkerLinker"]     = unpack('!B', file.read(1))[0]
            data["SizeOfCode"]            = unpack('<I', file.read(4))[0]
            data["SizeOfInitializedData"] = unpack('<I', file.read(4))[0]
            data["SizeOfUninitializedData"] = unpack('<I', file.read(4))[0]   # was missing
            data["AddressOfEntryPoint (RVA)"] = unpack('<I', file.read(4))[0]
            data["BaseOfCode (RVA)"]      = unpack('<I', file.read(4))[0]
            if not is_pe32plus:
                data["BaseOfData (RVA)"] = unpack('<I', file.read(4))[0]

            # Windows-specific fields
            data["ImageBase"]                     = unpack(pat, file.read(ptr_size))[0]
            data["SectionAlignment"]              = unpack('<I', file.read(4))[0]
            data["FileAlignment"]                 = unpack('<I', file.read(4))[0]
            data["MajorOperatingSystemVersion"]   = unpack('<H', file.read(2))[0]
            data["MinorOperatingSystemVersion"]   = unpack('<H', file.read(2))[0]
            data["MajorImageVersion"]             = unpack('<H', file.read(2))[0]
            data["MinorImageVersion"]             = unpack('<H', file.read(2))[0]
            data["MajorSubsystemVersion"]         = unpack('<H', file.read(2))[0]
            data["MinorSubsystemVersion"]         = unpack('<H', file.read(2))[0]
            data["Win32VersionValue"]             = unpack('<I', file.read(4))[0]
            data["SizeOfImage"]                   = unpack('<I', file.read(4))[0]
            data["SizeOfHeaders"]                 = unpack('<I', file.read(4))[0]
            data["CheckSum"]                      = unpack('<I', file.read(4))[0]
            data["Subsystem"]                     = unpack('<H', file.read(2))[0]
            data["DllCharacteristics"]            = unpack('<H', file.read(2))[0]
            data['SizeOfStackReserve']            = unpack(pat, file.read(ptr_size))[0]
            data['SizeOfStackCommit']             = unpack(pat, file.read(ptr_size))[0]
            data['SizeOfHeapReserve']             = unpack(pat, file.read(ptr_size))[0]
            data['SizeOfHeapCommit']              = unpack(pat, file.read(ptr_size))[0]
            data["LoaderFlags"]                   = unpack('<I', file.read(4))[0]
            data["#NumberOfRvaAndSizes"]          = unpack('<I', file.read(4))[0]

            # Data directories
            def _dd():
                rva  = unpack('<I', file.read(4))[0]
                size = unpack('<I', file.read(4))[0]
                return rva, size

            data["ExportTable (RVA)"],           data["SizeOfExportTable"]           = _dd()
            data["ImportTable (RVA)"],           data["SizeOfImportTable"]           = _dd()
            data["ResourceTable (RVA)"],         data["SizeOfResourceTable"]         = _dd()
            data["ExceptionTable (RVA)"],        data["SizeOfExceptionTable"]        = _dd()

            # ── Certificate table entry — record file offsets ────────────────
            data["CertificateTable Pointer location"]     = file.tell()
            data["CertificateTable (RVA)"]                = unpack('<I', file.read(4))[0]
            data["SizeOfCertificateTable Pointer location"] = file.tell()
            data["SizeOfCertificateTable"]                = unpack('<I', file.read(4))[0]

            data["BaseRelocationTable (RVA)"],   data["SizeOfBaseRelocationTable"]   = _dd()
            data["Debug (RVA)"],                 data["SizeOfDebug"]                 = _dd()
            data["ArchitectData (RVA)"],         data["SizeOfArchitectData"]         = _dd()
            data["GlobePtr(RVA)"],               _ = _dd()
            data["TLSTable (RVA)"],              data["SizeOfTLSTable"]              = _dd()
            data["LoadConfigTable (RVA)"],       data["SizeOfLoadConfigTable"]       = _dd()
            data["BoundImport (RVA)"],           data["SizeOfBoundImport"]           = _dd()
            data["ImportAddressTable (RVA)"],    data["SizeOfImportAddressTable"]    = _dd()
            data["DelayImportDescriptor (RVA)"], data["SizeOfDelayImportDescriptor"] = _dd()
            data["CLRRuntimeHeader (RVA)"],      data["SizeOfCLRRuntimeHeader"]      = _dd()

            file.seek(file.tell() + 8)          # reserved data directory

            # First section header  (40 bytes)
            data["Name"]                        = file.read(8)
            data["VirtualSize"]                 = unpack('<I', file.read(4))[0]
            data["VirtualAddress (RVA)"]        = unpack('<I', file.read(4))[0]
            data["SizeOfRawData"]               = unpack('<I', file.read(4))[0]
            data["PointerToRawData"]            = unpack('<I', file.read(4))[0]
            data["PointerToRelocations"]        = unpack('<I', file.read(4))[0]
            data["PointerToLinenumbers"]        = unpack('<I', file.read(4))[0]
            data["NumberOfRelocations"]         = unpack('<H', file.read(2))[0]
            data["NumberOfLinenumbers"]         = unpack('<H', file.read(2))[0]
            data["Characteristics (Section Table)"] = unpack('<I', file.read(4))[0]

        return data

    # ── public API ────────────────────────────────────────────────────────────

    def is_signed(self) -> bool:
        return bool(self.filedata["SizeOfCertificateTable"])

    def get_certificate(self) -> bytes:
        if not self.is_signed():
            raise CertificateError("Source file is not signed")
        with open(self.filepath, "rb") as f:
            f.seek(self.filedata["CertificateTable (RVA)"])
            return f.read(self.filedata["SizeOfCertificateTable"])

    def write_certificate(self, outputfile: str) -> None:
        """
        Append this object's certificate onto *outputfile*.
        outputfile must be unsigned (or have had its cert stripped first).
        """
        target = EXEAnalyzer(outputfile)
        if target.is_signed():
            raise CertificateError("Target file already signed — strip it first.")

        certificate = self.get_certificate()
        size_bytes  = pack('<I', self.filedata["SizeOfCertificateTable"])

        tbl_ptr  = target.filedata["CertificateTable Pointer location"]
        size_ptr = target.filedata["SizeOfCertificateTable Pointer location"]

        with open(outputfile, 'rb') as f:
            data = bytearray(f.read())

        cert_offset = len(data)                 # cert goes right at EOF
        data[tbl_ptr:tbl_ptr + 4]     = pack('<I', cert_offset)
        data[size_ptr:size_ptr + 4]   = size_bytes
        data += certificate

        with open(outputfile, 'wb') as f:
            f.write(data)

        # refresh cached metadata
        self.filedata = EXEAnalyzer(outputfile).filedata if outputfile == self.filepath \
                        else self.filedata

    def remove_certificate(self) -> None:
        """
        Strip the Authenticode certificate from this file in-place.
        FIX:  original had data[:loc] + data[loc + size + 1:]
              which skipped one extra byte after the cert block.
              Corrected to  data[:loc] + data[loc + size:]
        """
        if not self.is_signed():
            raise CertificateError("Source file is not signed")

        loc        = self.filedata["CertificateTable (RVA)"]
        size       = self.filedata["SizeOfCertificateTable"]
        loc_index  = self.filedata["CertificateTable Pointer location"]
        size_index = self.filedata["SizeOfCertificateTable Pointer location"]

        with open(self.filepath, "rb") as f:
            data = bytearray(f.read())

        # Zero out the data-directory pointers
        data[loc_index:loc_index + 4]   = pack("<I", 0)
        data[size_index:size_index + 4] = pack("<I", 0)

        # Remove the certificate block  (no +1 off-by-one)
        data = data[:loc] + data[loc + size:]

        with open(self.filepath, 'wb') as f:
            f.write(data)

        # Update cached state
        self.filedata["CertificateTable (RVA)"]  = 0
        self.filedata["SizeOfCertificateTable"]  = 0

    def __len__(self) -> int:
        with open(self.filepath, 'rb') as f:
            return len(f.read())


# ─── CLI (unchanged from original, kept for compatibility) ─────────────────

def main() -> None:
    parser = OptionParser()
    parser.add_option('-o', dest='outputfile')
    parser.add_option('-s', dest='sigfile')
    parser.add_option('--rm', dest='inputfile')
    (options, args) = parser.parse_args()

    outfile  = options.outputfile
    sigfile  = options.sigfile
    infile   = options.inputfile

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
