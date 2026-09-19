#!/usr/local/lib/mailinabox/env/bin/python


# Direct conversion from the perl original, Log::Saftpresse::Plugin::Postfix::Tls

import re

class PostfixTlsMixin:

    def process_tls(self, stash, notes):
        service = stash.get("service")
        pid = stash.get("pid")
        message = stash.get("message", "")
        queue_id = stash.get("queue_id")

        if service not in ("smtp", "smtpd"):
            return

        # Regex translation
        m = re.match(
            r"^(\S+) TLS connection established (?:from|to) ([^\[]+)\[([^\]]+)\]:(?:\d+:)? (\S+) "
            r"with cipher (\S+) \((\d+)\/(\d+) bits\)",
            message
        )

        if m:
            tlsLevel, tlsHost, tlsAddr, tlsProto, tlsCipher, tlsKeylen, _ignored = m.groups()

            tls_params = {
                "tls_level": tlsLevel,
                "tls_proto": tlsProto,
                "tls_chipher": tlsCipher,
                "tls_keylen": tlsKeylen,
            }

            self.incr_tls_stats(stash, tls_params, "tls_conn", service)

            # Copy into stash
            stash.update(tls_params)

            notes.set(f"{service}-tls-{pid}", tls_params)
            return

        # No new TLS connection, check stored params
        tls_params = notes.get(f"{service}-tls-{pid}")

        if tls_params is not None:
            if service == "smtpd":
                if re.match(r"^connect from", message):
                    notes.remove(f"{service}-tls-{pid}")
                    return

                elif re.match(r"^disconnect", message):
                    notes.remove(f"{service}-tls-{pid}")

                elif re.match(r"^client=", message):
                    self.incr_tls_stats(stash, tls_params, "tls_msg", service)

                stash.update(tls_params)

            elif service == "smtp" and re.search(r"status=(sent|bounced|deferred)", message):
                self.incr_tls_stats(stash, tls_params, "tls_msg", service)
                notes.remove(f"{service}-tls-{pid}")

                # postfix/smtp closes TLS after each delivery
                # but queue_id may have more recipients → store params under queue_id
                if queue_id is not None:
                    notes.set(f"{service}-tls-{queue_id}", tls_params)

        # No PID-based params, try queue-id-based
        elif queue_id is not None:
            tls_params = notes.get(f"{service}-tls-{queue_id}")
            if tls_params is not None:
                stash.update(tls_params)

        return

    def incr_tls_stats(self, stash, tls_params, *path):
        """
        Python version of incr_tls_stats
        """
        self.incr_host_one(stash, *path, "total")
        self.incr_host_one(stash, *path, "level", tls_params["tls_level"])
        self.incr_host_one(stash, *path, "proto", tls_params["tls_proto"])
        self.incr_host_one(stash, *path, "cipher", tls_params["tls_chipher"])
        self.incr_host_one(stash, *path, "keylen", tls_params["tls_keylen"])


# Counters created
  <host>.tls_msg.smtpd.cipher.<tls_cipher>
  <host>.tls_msg.smtpd.keylen.<tls_keylen>
  <host>.tls_msg.smtpd.total
  <host>.tls_msg.smtpd.level.<tls_level>
  <host>.tls_msg.smtpd.proto.<tls_version>
  <host>.tls_msg.smtp.cipher.<tls_cipher>
  <host>.tls_msg.smtp.keylen.<tls_keylen>
  <host>.tls_msg.smtp.total
  <host>.tls_msg.smtp.level.<tls_level>
  <host>.tls_msg.smtp.proto.<tls_procol>
  <host>.tls_conn.smtpd.cipher.<tls_cipher>
  <host>.tls_conn.smtpd.keylen.<tls_keylen>
  <host>.tls_conn.smtpd.total
  <host>.tls_conn.smtpd.level.<tls_level>
  <host>.tls_conn.smtpd.proto.<tls_proto>
  <host>.tls_conn.smtp.cipher.<tls_cipher>
  <host>.tls_conn.smtp.keylen.<tls_keylen>
  <host>.tls_conn.smtp.total
  <host>.tls_conn.smtp.level.<tls_level>
  <host>.tls_conn.smtp.proto.<tls_proto>
