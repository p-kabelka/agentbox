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

_fedora_mirrors := {
	"ask4.mm.fcix.net",
	"b4sh.mm.fcix.net",
	"distrib-coffee.ipsl.jussieu.fr",
	"distrohub.kyiv.ua",
	"divergentnetworks.mm.fcix.net",
	"eu.edge.kernel.org",
	"fedora.ip-connect.info",
	"fedora.ip-connect.vn.ua",
	"fedora.ipacct.com",
	"fedora.mirror-services.net",
	"fedora.mirror.liteserver.nl",
	"fedora.mirror.root.lu",
	"fedora.mirrorservice.org",
	"fedora.tu-chemnitz.de",
	"fr2.rpmfind.net",
	"ftp-stud.hs-esslingen.de",
	"ftp.cica.es",
	"ftp.fau.de",
	"ftp.otenet.gr",
	"ftp.sh.cvut.cz",
	"ftp.uni-bayreuth.de",
	"ftp.uni-stuttgart.de",
	"mirror.23m.com",
	"mirror.accum.se",
	"mirror.bahnhof.net",
	"mirror.dogado.de",
	"mirror.etf.bg.ac.rs",
	"mirror.i3d.net",
	"mirror.imt-systems.com",
	"mirror.in2p3.fr",
	"mirror.init7.net",
	"mirror.karneval.cz",
	"mirror.maeen.sa",
	"mirror.netsite.dk",
	"mirror.netzwerge.de",
	"mirror.nl.leaseweb.net",
	"mirror.plusline.net",
	"mirror.slu.cz",
	"mirror.telepoint.bg",
	"mirror.yandex.ru",
	"mirrors.chroot.ro",
	"mirrors.ircam.fr",
	"mirrors.n-ix.net",
	"mirrors.nxthost.com",
	"mirrors.xtom.de",
	"mirrors.xtom.ee",
	"repo.hyron.dev",
	"www.fedora.is",
	"www.nic.funet.fi",
}

_preset_allow if {
	input.request.host in _fedora_mirrors
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
