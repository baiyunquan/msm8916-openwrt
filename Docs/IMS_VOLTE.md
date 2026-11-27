# IMS registration and VoLTE on MSM8916 sticks

What it takes to get a Qualcomm MSM8916 modem stick registered on IMS and
making VoLTE calls under OpenWrt, and what we learned on the way (October and
November 2026). Two boards were worked on:

| Board | Modem build | RF | Result |
| --- | --- | --- | --- |
| UFI003 MB V02 | `MPSS.DPM.2.0.2.c1-00178` | WTR4905 | **Works**: IMS registered, MO/MT VoLTE calls, call waiting, audio to a BT headset and SIP phones |
| UFI103S v05 | `MPSS.DPM.1.0.C7-00193` | WTR1605 | **IMS registered; Voice service available; outgoing call reached active** after a device-local IMS EFS port. Audio and the exact SIP/RAT path during the call remain unverified. |

The rest of the call stack (audio, Bluetooth, Asterisk) is in
[UFI003_VOICE.md](UFI003_VOICE.md); the watchdog interplay is in
[Modem Stability/MODEM_BEARER_WATCHDOG.md](Modem%20Stability/MODEM_BEARER_WATCHDOG.md).
This page is about IMS and VoLTE itself.

Short version:

- The modem runs IMS itself (SIP, registration, VoLTE call manager, RTP,
  IPsec). Linux never sees SIP or RTP. Linux has to: enable IMS in the modem
  EFS, give the modem an `ims` PDN when it asks for one (hybrid mode), and
  open the DSP's VoLTE voice session for audio.
- IMS settings are modem-generation and carrier dependent. Do not activate a
  donor MBN across modem generations. On this UFI103S v05, an IMS-only subset
  of EFS files from the supplied UFI003 CMCC profile was accepted by the
  preserved DPM.1.0 baseband; the complete MBN was not activated.
- That device-local port now registers on China Unicom, exposes IMS Voice, and
  has completed an outgoing call to `active`. This evidence applies to this
  UFI103S, modem build and SIM; it does not establish compatibility for other
  DPM.1.0 sticks or carriers.

## 1. Architecture

```
          operator IMS core (P-CSCF)
                 ▲ SIP / RTP over the ims PDN (IPv6)
 modem ──────────┼──────────────────────────────────────────────┐
 │  IMS stack: registration, SIP, qipcall (VoLTE calls), RTP     │
 │     │ hybrid mode: "please bring up the ims PDN"              │
 │     ▼ QMI service 770 (IMS data connection, client side)      │
 │  Q6 DSP: VoLTE voice session (vocoder) ──▶ AFE ports          │
 └─────┼────────────────────────────────────────────────────────┘
       │ QRTR
 Linux ▼
   imsdcm (serves QMI 770) ──▶ ModemManager: bearer apn=ims ipv4v6
   ModemManager voice (QMI VOICE): dial / answer / hold / waiting
   voicectl: opens the VoLTE (or CS) DSP session, mixer routes
```

- **Modem-side IMS.** The DPM firmware contains Qualcomm's full IMS client
  (`modem_proc/ims`: registration manager, SIP over UDP/TCP, P-CSCF
  discovery via PCO/DHCP/ISIM, IPsec, `qipcall`, `qvp_rtp` with AMR). It
  reads its settings from EFS items under `/nv/item_files/ims/`, normally
  provided by the active MCFG (carrier configuration).
- **Internal vs hybrid.** In internal mode the IMS stack opens the `ims` PDN
  with the modem's own data services. In hybrid mode
  (`ims_hybrid_enable` = 1) it asks an AP-side **IMS data connection
  service** (QMI service 770, "DCM") to do it. This is what Android's
  `imsdatadaemon` / postmarketOS `81voltd` serve. On the UFI003 we use hybrid
  mode with our own `imsdcm`, so the bearer goes through ModemManager and
  netifd sees it.
- **Call control** stays in ModemManager (QMI VOICE). A VoLTE call looks
  like any other call to ModemManager.
- **Audio.** VoLTE voice is decoded in the DSP, on its own **VoLTE voice
  session**. A VoLTE call has audio only on the VoLTE session (the CS
  session records silence), a CS call only on the CS session.

## 2. UFI003 recipe (DPM.2.0.2)

### 2.1 Modem profile

Use the generic ROW MCFG referenced by `volte-setup`; it must include IMS
support and the settings required by the target network. Keep network-specific
profile data device-local and out of shared configuration or documentation.

### 2.2 EFS items

| EFS item | Value | Source |
| --- | --- | --- |
| `/nv/item_files/ims/IMS_enable` | `01` | written |
| `/nv/item_files/ims/ims_operation_mode` | `02` | from a compatible modem profile |
| `/nv/item_files/ims/ims_hybrid_enable` | `01` (AP provides the PDN) | written |
| `/nv/item_files/ims/qp_ims_reg_config` | 405 bytes; APN `ims` at offset 2 | a compatible modem profile leaves the APN empty; written |
| `/nv/item_files/modem/mmode/voice_domain_pref` | `03` (IMS preferred), **exactly 1 byte** | written |

The `voice_domain_pref` size matters. The ROW MCFG leaves a 2-byte item;
with it the modem registers IMS for **SMS only** (IMS registered, voice
service unavailable, calls on 3G).

`volte-setup` (package `ufi-voice`) does all of it:

```sh
volte-setup status            # MCFG, the items, IMS registration, SMS/voice service
volte-setup apply --restart   # write what differs, sync the EFS, restart the modem
```

On **UFI103S v05**, `ims-profile-setup` checks and persists the two IMS enable
bytes. It does not overwrite `voice_domain_pref`, which is carrier- and
user-specific; that preference must be applied per device. Other UFI voice
boards keep their existing EFS profile. The `ufi-voice` package includes
`qmi-raw`, which the IMS profile setup uses to set the WDS IMCN and
PCSCF-via-PCO flags.

How it works, in case it has to be done by hand:

- EFS access goes over DIAG with `diag_efs` (package `diag-logtool`),
  always under `flock /var/lock/himi-ok-diag.lock`, the same lock as the
  NV 2500 guard. Two DIAG users at once hang `/dev/diag` writes.
- **EFS writes live in the modem's RAM copy** until the modem syncs EFS to
  `modemst1/2` through `rmt_storage`. A reboot before the sync is accepted can
  lose it. `volte-setup` requests a journal sync with
  `flock $LOCK diag_logtool efs-sync`; the command checks the modem's EFS
  result, rather than only matching a generic DIAG response byte. The modem's
  later sync-status response is not reliable on this firmware, so acceptance
  is confirmed and completion is treated as best-effort.
- The modem reads the items when it starts: restart it afterwards
  (`echo stop`/`echo start` to the `4080000.remoteproc` remoteproc's
  `state`, or reboot).
- `qp_ims_reg_config` patch: if the item is 405 bytes and bytes 2-4 are
  zero, insert `69 6d 73` ("ims") at offset 2 and keep the rest. If the item
  is missing or has another size, the right MCFG is not active; do not
  write one from scratch.

### 2.3 UFI103S v05 IMS-only profile port

The target's preserved baseband is `MPSS.DPM.1.0.C7-00193`
(`UFI103_CT 20220801`). The supplied UFI003 package's
`Commercial-CSFB-SS-CMCC.mbn` contains 40 IMS EFS records. The target's own
`conf/ims.conf` supports 38 of those paths; two unsupported QIPCALL records
were excluded. The source MBN SHA-256 is
`b43791bc25518c8495ec5731318801a6fbd02fd19c562e816433d51b10c89201`.

The port wrote only those supported IMS EFS records (6,819 bytes). The two
enable flags already matched `01`; the other 36 files were absent. No MBN,
whole EFS image, `modemst`, `fsg`, firmware, RF calibration or device identity
was copied. EFS writes were read back and synced. The device-local rollback
journal is under `/root/ufi103-ims-port/`.

The port also required `/nv/item_files/modem/mmode/voice_domain_pref=03` as an
exact one-byte PS-preferred setting. That item was absent before the change;
`volte-setup apply` wrote it and the modem accepted the EFS sync. The value
persists in this device's EFS; the generic boot hook does not force it on
other SIMs or users.

On 2026-11-27, after a modem restart, QMI service 770 was published before the
modem attached. It received the IMS request for WDS profile 3, created an IPv6
IMS bearer on `wwan1`, and the IMS registration reader reported `registered`;
QMI IMS Voice was `available`. The separate `3gnet` bearer remained on
`wwan0`, and an IPv4 ping through `wwan0` succeeded. An outgoing call reached
`active` and was hung up cleanly. SIP packets and call-time RAT were not
captured, and no audio endpoint was connected, so audio and independent proof
of the SIP signaling path remain unverified. A full AP cold-boot cycle has not
yet been repeated with the final startup ordering.

### 2.4 Verify

```sh
qmicli -d qrtr://0 --imsa-get-ims-registration-status   # Status: 'registered'
qmicli -d qrtr://0 --imsa-get-ims-services-status       # Voice service: available
mmcli -m any --list-bearers                             # one bearer has apn ims
logread -e imsdcm                                       # start/stop requests, address
```

Registered with only the SMS service available means `voice_domain_pref` is
wrong (§2.2). If IMS Voice is available but a call still falls back to CS,
check the modem's voice-domain/network behavior; `ufivoice.main.session`
selects the DSP audio session and does not select the network call domain (§5).

Use `qmicli` on `qrtr://0` sparingly from scripts: every call takes a QMI
client id. A polling loop has exhausted client ids before
(`ClientIdsExhausted`).

## 3. imsdcm: the AP side of the IMS data connection

`/usr/sbin/imsdcm` (Python, procd service `imsdcm`, enabled by
`ufivoice.main.volte`) serves QMI service 770 version 1 instance 0 over
`AF_QIPCRTR`. The message layout follows postmarketOS OpenIMSD's `81voltd`
(`imsd.qmi`).

### 3.1 Protocol, as this modem uses it

| Message | Direction | What we do |
| --- | --- | --- |
| 0x20 start connection (APN, IP family, WDS profile) | modem → AP | `mmcli --create-bearer apn=ims,ip-type=ipv4v6,profile-id=<requested>` + connect. Response TLVs 0x10 (PDP ID, u8), 0x11 (connection handle), 0x12 (subscription). Then indication 0x20 with the address in TLV 0x11 `{u32 family, u8 len, string}` |
| connection down | AP → modem | indication 0x20 with result error 13 and no address |
| 0x21 stop connection | modem → AP | response TLV 0x10 echoes the allocated PDP ID, TLV 0x11 echoes the handle; tear the bearer down, then **report it down**: without the down indication the modem never asks again |
| 0x23 and the rest | modem → AP | plain success, no action |

- The AP reserves PDP ID zero for failure and allocates IDs 1 through 255.
  Stop requests are matched by the returned PDP ID; responses echo that ID.
- The P-CSCF address comes to the modem in the network's PCO; the AP does
  not have to pass it.

### 3.2 Start-up order

`imsdcm` publishes service 770 immediately. This matters on UFI103S v05:
the DPM.1.0 IMS client sends one Start Connection request during attach and
does not retry if service 770 is absent. For a new UFI103S request, the
handler waits up to 60 seconds for ModemManager attachment, deletes leftover
IMS bearers, and waits up to 30 seconds for the primary non-IMS bearer on
`wwan0` to remain ready for five seconds. It then creates the IMS bearer using
the WDS profile ID requested by the modem. If no primary bearer appears, it
continues for voice-only use. This preserves `wwan0` for Internet and keeps
IMS on `wwan1` when both PDNs are active.

Other boards also publish immediately; their request handling does not wait
for the UFI103S-specific primary-data condition.

### 3.3 Keeping it alive

- Connections are kept in `/tmp/imsdcm.state`. A new instance reports the
  old ones down to the modem first, so the modem asks again.
- A watchdog every 15 s:
  - a bearer that disappeared is reported down at once;
  - **a ModemManager modem path change** (modem restart/SSR) makes `imsdcm`
    exit; procd starts a fresh instance that immediately republishes service
    770 (§3.2). The request handler waits for ModemManager to finish probing
    before it creates the IMS bearer;
  - idle (no connection and IMS not registered) for `ims_kick_after` seconds
    (180) → exit and restart. The limit doubles each time it does not help
    (`/tmp/imsdcm.kick`, up to 3600 s), e.g. on a network without VoLTE.
- A failed connect forgets the bearer, so the next request creates a new
  one.
- A connection that went away is dropped from the table at once. A stale
  entry made the old watchdog believe IMS had its PDN: IMS stayed down and
  calls fell back to 3G.
- The socket loop uses `select()`. A blocking `recvfrom()` holds the QRTR
  socket lock and blocks the indications sent from the watchdog thread.
- The early IMS "kick" (`--set-power-state-low` and back) made ModemManager
  disable the modem; it was replaced by the service restart above.
- Manual recovery if IMS stays down after a modem crash:
  `/etc/init.d/imsdcm restart`.

### 3.4 ModemManager and netifd

- OpenWrt's dispatcher `/usr/lib/ModemManager/connection.d/10-report-down`
  restarts the WAN interface on **any** bearer disconnect. Restarting the
  interface disconnects every bearer, IMS included, which disconnects again:
  a reconnect loop. `ufi-voice` keeps the `/etc` hook, and the package overlay
  replaces the packaged `/usr/lib` hook; both ignore the `ims` bearer and
  bearers of other interfaces.
- The `ims` bearer is not a netifd interface; nothing routes over it on the
  Linux side. The modem carries SIP/RTP itself.
- The internet bearer (netifd `modem`) and the `ims` bearer are separate
  PDNs. The ims PDN works with a voice-only SIM that has no data service.

## 4. Network-specific configuration

IMS registration depends on the service provider's provisioning. Keep
network-specific APN and proxy settings in local device configuration rather
than in a shared image or source tree. The `ims` APN name used by the modem
is a generic default; do not replace it unless the network requires a
different value. Supplementary call features may also vary by service.

## 5. Calls over VoLTE

- **Session choice:** `ufivoice.main.session 'auto'` = VoLTE session while
  IMS is registered, CS otherwise. Wrong session = silence.
- **Clean uplink to SIP phones:** in `host` mode the DSP's vocproc TX input is
  the unused BT SCO port. Its noise went into VoLTE calls word by word (the
  codec's DTX gates it). `Voice Tx Mute` (host_tx_mute, default 1) mutes it;
  the in-call playback is mixed in after the mute.
- **Incoming calls** need a registered QMI VOICE client (ModemManager is
  one); an AT-only dialer never confirms the call setup.
- **Call waiting** with the same number: ModemManager merged a waiting call
  into the active one by number (patch 100 in the overlay fixes it).
  Linphone answers 486 to a second call from the same address, so the bridge
  offers it as `<number>~2`.
- With IMS unregistered (no LTE, network without VoLTE, PDN down) calls fall
  back to CSFB/3G and still work.

## 6. Modem profile compatibility

Modem firmware generations can use different profile formats and EFS layouts.
A profile or EFS item from another device should not be copied blindly: check
that it matches the modem firmware and intended service, and preserve a
recoverable backup before making device-local changes.

## 7. Safety and privacy

- **Back up EFS before writes**: `modemst1`, `modemst2` (and `fsg`) as raw
  partition images. A bad MCFG activation can crash-loop the modem before QMI
  is up; only a partition restore fixes it.
- Do not activate MBNs from another modem generation.
- Keep the NV 2500 HiMI_OK guard (HiMI firmware) and its DIAG lock; space
  DIAG requests (`sleep 1`, timeouts).
- Never flash modem/firmware partitions casually; IMS needs none of that on
  the UFI003.
- EFS dumps, MCFG state (`mcfg_last_autoselect_iccid`), QMI/IMS logs and
  Asterisk logs contain the IMEI, IMSI, ICCID, phone number, IPv6 interface
  ids and P-CSCF addresses. Keep them on the device or in private storage;
  mask them in anything shared.

## 8. References

- postmarketOS OpenIMSD (`81voltd`, `qcom-imsd`): https://www.openimsd.de/pages/architecture.html
- MCFG MBN format: fenrir-naru/mbn_utils
- ASUS MSM8916 VoLTE MBNs: TheMuppets `proprietary_vendor_asus`,
  `8916_volte_modem/modem_pr/mcfg/configs/mcfg_sw/`
- This repo: `packages/ufi-voice` (`imsdcm`, `volte-setup`, `10-report-down`),
  `packages/diag-logtool`, `msm89xx/patches/873`–`875`,
  [UFI003_VOICE.md](UFI003_VOICE.md), [UFI003_MB_V02.md](UFI003_MB_V02.md)

## UFI103S v05 bring-up record (2026-11-27)

This board's preserved baseband is `MPSS.DPM.1.0.C7-00193`
(`UFI103_CT 20220801`), not the DPM.2.0.2 baseband used in the UFI003 recipe.
The linked AirSimHub `docs/UNICOM-VOICE.md` records a different UFI003 baseband
and its own verified Gen_3GPP profile. Its private modem shadow profile is not
part of the public source and must not be copied between devices.

Confirmed on this UFI103S:

- LTE data works; NAS reports IMS voice support from the network.
- `IMS_enable=01` and `ims_hybrid_enable=01` persist and expose IMS QMI services.
- Existing `ims` APN entries lose their IMCN/PCO flags on modem restart. The
  setup now checks, repairs and verifies them even when the APN already exists.
- The new UFI103S-only policy follows AirSimHub's Unicom runtime NAS preference
  (46001/46009, PS preferred, POWER_CYCLE, no active calls). It retries after
  ModemManager becomes ready and after modem SSR.
- Repeated `a2_power.c:1189` crashes occurred with BAM autosuspend. Keeping its
  power vote on stopped that crash during the observed run. The board DTS now
  uses the driver's deferred `qcom,a2-pin` mechanism. The runtime fallback only
  pins an already-active device, after its power handshake.
- IMSA **registration** query is service 0x21/message **0x20**. Message **0x21**
  queries services. Some older SOP examples reversed them. DPM.1.0 returns
  registration as the legacy one-byte TLV 0x10 boolean, which current qmicli
  doesn't display; `ufi-ims-policy --status` supports it and modern TLV 0x12.

Before the IMS-only profile port below, IMS was **not registered** and Voice
service was **unavailable**.
The legacy IMS settings getters for registration manager (0x26), QIPCALL
(0x37), and extended registration (0x45) return QMI error 54 with settings
cause 3 (`READ_FAILED`); relevant IMS EFS configuration is absent. Modern
0x8f/0x90 services-enabled settings are rejected with QMI error 58.

A trial of the shipped 24772-byte Gen_3GPP MCFG uploaded successfully but
stayed Pending after activation, with no missing IMS configuration populated.
It was deactivated and deleted. Temporary auto-selection/operation-mode and
QIPCALL configuration experiments were also removed, with EFS sync accepted.
Those experiments did not establish registration; the later successful path
used an IMS-only EFS subset from the supplied UFI003 package and did not
activate that donor MCFG. No radio calibration or firmware partition was
changed.

Protocol references: [libqmi IMSA message definitions](https://github.com/linux-mobile-broadband/libqmi/blob/main/data/qmi-service-imsa.json)
and [Qualcomm IMS settings definitions](https://github.com/bcyj/android_tools_leeco_msm8996/blob/f242858a8cbb2c9ad9b5b2e53d75380ae775727a/qmi/services/ip_multimedia_subsystem_settings_v01.h).

### Downloaded stock package inspected

`ufi103s_v05_stock.zip` was supplied afterwards. It is a downloaded same-model
factory image, not this unit's private backup. Its modem.b18 SHA-256 is
`6af34188cd0518bce52e62a4968965a188c73e9346b42de33c23c17c0607adbb`, identical
to the firmware already running. Read-only inspection of system and userdata
found the vendor's 12 modem test/selection MBNs (`ct`, CMCC, CTA, GCF), but none
contains IMS EFS configuration. There is no Gen_3GPP/IMS profile in its modem
FAT partition. Its modemst/fsg/persist data was not applied to this unit.
The archive therefore provides provenance for the current baseband, but no
validated missing IMS configuration or newer VoLTE baseband.


### IMS-only profile port from the supplied UFI003 NV package

The supplied `bin/fsg.bin` contains a gzip-compressed tar at offset `0x228`,
with six standalone MCFGs under `/nvm/mcfg/cfg_01`. Their names alone are not
proof of IMS support: the CU (Unicom), CT and ROW files in that particular
package carry no IMS EFS records. `Commercial-CSFB-SS-CMCC` has 40 IMS file
records; Reliance/Jio has 47 and is a different carrier configuration.

For this UFI103S's preserved DPM.1.0 baseband, only the CMCC MCFG's IMS file
records were extracted. Its SHA-256 is
`b43791bc25518c8495ec5731318801a6fbd02fd19c562e816433d51b10c89201`.
The extracted files were intersected with the target's own `conf/ims.conf`:
`qipcall_dtx_enabled` and `qipcall_qos_config` were excluded as unsupported.
`IMS_enable` was overridden to `01`; `ims_hybrid_enable=01`, operation mode
`02` and the `ims` APN were retained. This is an IMS-only adaptation of those
configuration records, not activation of the complete CMCC carrier MCFG.

The resulting local bundle has 38 files, 6,819 bytes. The two enable flags
already matched, so 36 absent IMS files were written. Every write was read
back and matched before EFS sync and restart. A per-file rollback journal
records both originally absent files and existing preimages. The device's
firmware, RF calibration, SIM identity, modemst/fsg partition images and
carrier selection were not replaced with data from another device.

After the port, legacy IMS settings Get Registration Manager (0x26) and
Get QIPCALL (0x37) succeed, with VoLTE enabled, instead of READ_FAILED. An IMS
IPv6 PDN can be established, and China Unicom IMS registration plus Voice
service availability were observed. The previously absent one-byte
`voice_domain_pref=03` was then written and its EFS sync accepted.

An early modem-restart attempt exposed a service-start race: `imsdcm` waited
for ModemManager and the Internet bearer before publishing service 770, so the
baseband's one-time request arrived too early. The service now publishes
immediately, waits for attachment and `wwan0` data inside the request handler,
then creates the IMS bearer on `wwan1`. A live modem restart with this ordering
was successful: the IMS request arrived after publication, registration
returned, and Voice service was available. The Internet bearer remained on
`wwan0` and passed a two-packet IPv4 ping. A new outgoing call reached active
and was hung up cleanly. SIP packets and call-time RAT were not captured, and
audio was not verified because no audio endpoint was connected. A full AP
cold-boot cycle has not yet been repeated with the final startup ordering.

The AP connection service also needed protocol corrections:

- Preserve the requested LTE/EPC WDS profile number in the ModemManager
  bearer, rather than supplying only `apn=ims`. Otherwise the profile's
  IMCN and P-CSCF-via-PCO settings are lost. Bearers are keyed by APN and
  profile so a request cannot silently reuse another profile's session.
- Allocate and reuse PDP IDs 1 through 255; zero indicates activation
  failure. Return an error when all IDs are occupied. Deactivation responses
  echo the actual PDP ID, rather than the old fixed `0xfa` value.
- On UFI103S, publish service 770 before ModemManager attach so the baseband's
  one-time request is not lost. The request handler waits up to 60 seconds
  for attach, clears an old IMS bearer, and allows the non-IMS bearer on
  `wwan0` to settle for five seconds (up to 30 seconds total) before creating
  IMS. Giving IMS `wwan0` first left the Internet bearer on `wwan1` without
  traffic; with Internet first, both bearers stayed active and `wwan0` passed
  traffic. The bounded wait still allows voice-only use when there is no
  Internet bearer.
- Patch the packaged `/usr/lib/ModemManager/connection.d/10-report-down`
  as well as the `/etc` hook. ModemManager executes hooks from both
  directories; a same-named `/etc` file does not replace the packaged hook.
  Updating only `ufi-voice` on an old image will not update that `/usr/lib`
  hook; a rebuilt modemmanager package or full image is required.

Protocol evidence: [Qualcomm IMSDCM definitions](https://github.com/bcyj/android_tools_leeco_msm8996/blob/master/qmi/services/ip_multimedia_subsystem_dcm_v01.h),
[libqmi IMSDCM layout](https://github.com/linux-mobile-broadband/libqmi/blob/main/data/qmi-service-imsdcm.json),
and [ModemManager dispatcher implementation](https://github.com/linux-mobile-broadband/ModemManager/blob/main/src/mm-dispatcher-connection.c).
