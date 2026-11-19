/*
 * Minimal WHEP (WebRTC-HTTP Egress Protocol) player client.
 *
 * Adapted from Feedboard's src/lib/whep-client.ts
 *   (Feedboard, MIT License, Copyright (c) 2025 Karl Swanson; full text in alitech-live/THIRD_PARTY.md)
 *   Permission is hereby granted, free of charge, to any person obtaining a copy of this
 *   software ... THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
 *
 * Changes for AI Camera: plain JS (no build step); video only (shop cameras carry no
 * audio); no public STUN server (it would tell a third party the viewer's address); the
 * token stays in memory only and is never put in a URL; DELETE on close frees the session.
 */
(() => {
  "use strict";

  class WhepClient {
    constructor(url, token) {
      this.url = url;
      this.token = token;
      this.pc = null;
      this.resourceUrl = null;
      this.onstate = null;
    }

    async connect() {
      const pc = (this.pc = new RTCPeerConnection({ iceServers: [] }));
      pc.addTransceiver("video", { direction: "recvonly" });
      const stream = new MediaStream();
      pc.ontrack = (ev) => { if (!stream.getTracks().includes(ev.track)) stream.addTrack(ev.track); };
      pc.onconnectionstatechange = () => this.onstate && this.onstate(pc.connectionState);

      await pc.setLocalDescription(await pc.createOffer());
      await this.#iceGathered();

      const res = await fetch(this.url, {
        method: "POST",
        headers: { "Content-Type": "application/sdp", Authorization: `Bearer ${this.token}` },
        body: pc.localDescription.sdp,
        credentials: "omit",
        cache: "no-store",
        referrerPolicy: "no-referrer",
      });
      if (!res.ok) throw new Error(`whep_${res.status}`);
      const loc = res.headers.get("Location");
      if (loc) this.resourceUrl = new URL(loc, this.url).toString();
      await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
      pc.getReceivers().forEach((r) => { if (r.track && !stream.getTracks().includes(r.track)) stream.addTrack(r.track); });
      return stream;
    }

    #iceGathered() {
      return new Promise((resolve) => {
        if (this.pc.iceGatheringState === "complete") return resolve();
        const done = () => { if (this.pc && this.pc.iceGatheringState === "complete") { this.pc.removeEventListener("icegatheringstatechange", done); resolve(); } };
        this.pc.addEventListener("icegatheringstatechange", done);
        setTimeout(resolve, 1500);
      });
    }

    close() {
      if (this.pc) { this.pc.close(); this.pc = null; }
      if (this.resourceUrl) {
        fetch(this.resourceUrl, { method: "DELETE", headers: { Authorization: `Bearer ${this.token}` }, credentials: "omit", keepalive: true }).catch(() => {});
        this.resourceUrl = null;
      }
      this.token = null;
    }
  }

  window.WhepClient = WhepClient;
})();
