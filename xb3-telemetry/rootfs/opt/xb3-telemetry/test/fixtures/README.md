# Parser fixture provenance

These fixtures derive from complete and partially populated responses observed
on an ARRIS TG1682P PHP gateway interface. Network identifiers were removed.
Captured timestamps and software-version values were replaced with stable test
values before publication. Blank fields remain blank.

The retained channel, codeword, and reported-memory examples exercise the
observed layout and numeric shapes. They are parser inputs, not an incident
record or evidence of a current fault. Tests make explicit mutations to these
fixtures to check malformed cells, missing sections, and false defaults.

The Firewall fixture preserves original field names, Count strings and source
whitespace. Its endpoint fields contain the gateway-provided `0.0.0.0`
placeholders. Repeated snapshots must not be interpreted as new individual
events or accumulated drop counts.
