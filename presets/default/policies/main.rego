package agentbox

import rego.v1

default allow := false

# Deny overrides allow: if any deny rule matches, the request is blocked
allow if {
	count(_allow_rules) > 0
	not _any_deny
}

_any_deny if {
	deny[_]
}

# Collect all allow votes (from this file and others via incremental definition)
_allow_rules contains true if {
	_preset_allow
}

# Preset allowlist rules — override these by adding a deny rule, not by editing this file

_preset_allow if {
	input.request.host in {"pypi.org", "files.pythonhosted.org", "registry.npmjs.org"}
	input.request.method == "GET"
}

_preset_allow if {
	regex.match(`^.*\.fedoraproject\.org$`, input.request.host)
	input.request.method == "GET"
}

# Denial reasons for debugging

denial_reasons contains reason if {
	some host in deny
	reason := sprintf("host '%s' is explicitly denied", [host])
}

denial_reasons contains reason if {
	count(_allow_rules) == 0
	not _any_deny
	reason := "no policy rule matched this request"
}
