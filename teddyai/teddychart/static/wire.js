// wire.js — the binary twin of server.py's frame formats. Little-endian.
//
//   0x01 TRADES  u8 type, u8 flags, u16 pad, u32 n, f64 seq0,
//                n x { f64 ts_ms, f32 px, u32 sz, u8 side, u8[3] pad }   (20 bytes)
//   0x02 BOOK    u8 type, u8[7] pad, f64 ts_ms,
//                10 x { f32 bid_px, u32 bid_sz }, 10 x { f32 ask_px, u32 ask_sz }
//
// Decoding goes through DataView on purpose: the record stride is 20 bytes, so
// a Float64Array view over the payload would be misaligned, and DataView reads
// of a 40k-print frame cost about a millisecond.

export const TRADES = 0x01, BOOK = 0x02;
export const REC = 20, HEAD = 16;

export function decode(buf) {
  const dv = new DataView(buf);
  const type = dv.getUint8(0);
  if (type === TRADES) {
    const flags = dv.getUint8(1);
    const n = dv.getUint32(4, true);
    const seq0 = dv.getFloat64(8, true);
    const ts = new Float64Array(n), px = new Float32Array(n),
          sz = new Uint32Array(n), side = new Uint8Array(n);
    let o = HEAD;
    for (let i = 0; i < n; i++, o += REC) {
      ts[i] = dv.getFloat64(o, true);
      px[i] = dv.getFloat32(o + 8, true);
      sz[i] = dv.getUint32(o + 12, true);
      side[i] = dv.getUint8(o + 16);
    }
    return { type, replay: !!(flags & 1), n, seq0, ts, px, sz, side };
  }
  if (type === BOOK) {
    const ts = dv.getFloat64(8, true);
    const px = new Float32Array(20), sz = new Uint32Array(20);
    let o = 16;
    for (let i = 0; i < 20; i++, o += 8) {
      px[i] = dv.getFloat32(o, true);
      sz[i] = dv.getUint32(o + 4, true);
    }
    return { type, ts, px, sz };   // 0-9 bids best-first, 10-19 asks best-first
  }
  return { type };
}
