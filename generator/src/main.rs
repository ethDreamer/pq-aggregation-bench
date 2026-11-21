use lean_xmss::internals::*;
use lean_xmss::{LeafIndex, LeanXmssPublicKey, LeanXmssSignature, verify};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;
mod upstream_wots;

type Randomizer = [u8; RANDOMIZER_LEN];
const ZERO_DIGITS: usize = 6;
const MAX_RANDOMIZER_TRIALS: u64 = 1 << 32;
const TWEAK_PRF: u8 = 0;
const TWEAK_RANDOMIZER: u8 = 7;
const SIGNATURE_BYTES: usize = 2404;
const RECORD_BYTES: usize = 32 + 32 + SIGNATURE_BYTES;
const MAX_RECORDS: usize = 32768;
const MAX_PRIOR_SLOT_RECORDS: usize = 30720;
const CURRENT_LEAF: u32 = 32768;

// Helpers used by the unmodified upstream WOTS signer (private in its crate).
fn th_digests(pp: &PublicParam, tw: &Tweak, values: &[Digest]) -> Digest {
    th(pp, tw, values.as_flattened())
}
fn th_pair(pp: &PublicParam, tw: &Tweak, seed: &SchemeSeed) -> [Digest; 2] {
    let bytes = hasher(pp, tw).update(seed.as_bytes()).finalize();
    let mut pair = [[0; N]; 2];
    pair[0].copy_from_slice(&bytes[..N]);
    pair[1].copy_from_slice(&bytes[N..]);
    pair
}

// Only the chosen leaf exists as a signable key. All other subtrees are opaque,
// domain-separated commitments. Heights and verification work are unchanged.
fn sparse_path(seed: &[u8; 32], layer: usize, leaf: u32) -> [Digest; LAYER_HEIGHT] {
    std::array::from_fn(|height| {
        let mut preimage = b"BENCHMARK-ONLY-XMSS-UNOPENABLE-SUBTREE".to_vec();
        preimage.extend_from_slice(seed);
        preimage.extend_from_slice(&(layer as u64).to_le_bytes());
        preimage.extend_from_slice(&leaf.to_le_bytes());
        preimage.extend_from_slice(&(height as u64).to_le_bytes());
        keccak256::truncate(keccak256::Hasher::new().update(&preimage).finalize())
    })
}

fn root(
    pp: &PublicParam,
    layer: usize,
    leaf: u32,
    value: Digest,
    path: &[Digest; LAYER_HEIGHT],
) -> Digest {
    let mut value = value;
    let mut index = leaf;
    for (height, sibling) in path.iter().enumerate() {
        let children = if index & 1 == 0 {
            [value, *sibling]
        } else {
            [*sibling, value]
        };
        index >>= 1;
        value = th_digests(
            pp,
            &tweak(TWEAK_NODE, layer_hi(layer, (height + 1) as u32), index),
            &children,
        );
    }
    value
}

fn fixture(seed: &[u8; 32], message: &[u8; 32], leaf: u32) -> Result<Vec<u8>, String> {
    let scheme_seed = scheme_seed(seed);
    let pp = public_param(&scheme_seed);
    let bottom_path = sparse_path(seed, 0, leaf);
    let bottom_leaf = upstream_wots::wots_public_leaf(&pp, &scheme_seed, 0, leaf);
    let bottom_root = root(&pp, 0, leaf, bottom_leaf, &bottom_path);
    let top_index = layer_index(leaf, 1);
    let top_path = sparse_path(seed, 1, top_index);
    let top_leaf = upstream_wots::wots_public_leaf(&pp, &scheme_seed, 1, top_index);
    let top_root = root(&pp, 1, top_index, top_leaf, &top_path);
    let mut pk_bytes = [0; 32];
    pk_bytes[..N].copy_from_slice(&top_root);
    pk_bytes[N..].copy_from_slice(&pp);
    let mut signature = Vec::with_capacity(SIGNATURE_BYTES);
    signature.extend_from_slice(&leaf.to_le_bytes());
    for (layer, index, signed, path) in [
        (0, leaf, message.as_slice(), &bottom_path),
        (1, top_index, bottom_root.as_slice(), &top_path),
    ] {
        let (randomizer, values) =
            upstream_wots::wots_sign(&pp, &scheme_seed, layer, index, signed)
                .ok_or("no codeword")?;
        signature.extend_from_slice(&randomizer);
        signature.extend_from_slice(values.as_flattened());
        signature.extend_from_slice(path.as_flattened());
    }
    let sig_bytes = signature
        .as_slice()
        .try_into()
        .map_err(|_| "wrong signature size")?;
    check_signature(&pk_bytes, message, sig_bytes)?;
    let mut result = Vec::with_capacity(RECORD_BYTES);
    result.extend_from_slice(&pk_bytes);
    result.extend_from_slice(message);
    result.extend_from_slice(&signature);
    Ok(result)
}

fn check_signature(
    key: &[u8; 32],
    message: &[u8; 32],
    signature: &[u8; SIGNATURE_BYTES],
) -> Result<(), String> {
    let pk = LeanXmssPublicKey::from_bytes(key);
    let sig = LeanXmssSignature::from_bytes(signature);
    if !verify(&pk, message, &sig) {
        return Err("pinned verifier rejected sparse signature".into());
    }
    let mut wrong_message = *message;
    wrong_message[0] ^= 1;
    if verify(&pk, &wrong_message, &sig) {
        return Err("wrong message accepted".into());
    }
    Ok(())
}

fn verify_pack(path: &Path, threads: usize) -> Result<(), String> {
    let payload = fs::read(path).map_err(|e| e.to_string())?;
    let count = payload.len() / RECORD_BYTES;
    if threads == 0
        || count == 0
        || count > MAX_RECORDS + 2 * MAX_PRIOR_SLOT_RECORDS
        || payload.len() % RECORD_BYTES != 0
    {
        return Err("invalid fixture pack size or thread count".into());
    }
    let records_per_worker = count.div_ceil(threads);
    std::thread::scope(|scope| {
        let workers: Vec<_> = payload
            .chunks(records_per_worker * RECORD_BYTES)
            .enumerate()
            .map(|(batch, bytes)| {
                scope.spawn(move || {
                    for (offset, record) in bytes.chunks_exact(RECORD_BYTES).enumerate() {
                        let (header, signature) =
                            record.split_at_checked(64).ok_or("wrong record size")?;
                        let (key, message) =
                            header.split_at_checked(32).ok_or("wrong header size")?;
                        check_signature(
                            key.try_into().map_err(|_| "wrong key size")?,
                            message.try_into().map_err(|_| "wrong message size")?,
                            signature.try_into().map_err(|_| "wrong signature size")?,
                        )
                        .map_err(|error| {
                            let index = batch * records_per_worker + offset;
                            format!("fixture {index}: {error}")
                        })?;
                    }
                    Ok::<(), String>(())
                })
            })
            .collect();
        for worker in workers {
            worker.join().map_err(|_| "fixture verifier panicked")??;
        }
        Ok::<(), String>(())
    })?;
    println!("Verified {count} fixtures and rejected {count} wrong messages");
    Ok(())
}

fn main() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    if let [_, option, path, threads] = args.as_slice()
        && option == "--verify-pack"
    {
        return verify_pack(Path::new(path), threads.parse().map_err(|_| "bad threads")?);
    }
    let directory = PathBuf::from(
        args.get(1)
            .ok_or("expected directory count threads messages")?,
    );
    let count: usize = args
        .get(2)
        .ok_or("missing count")?
        .parse()
        .map_err(|_| "bad count")?;
    let threads: usize = args
        .get(3)
        .ok_or("missing threads")?
        .parse()
        .map_err(|_| "bad threads")?;
    let messages: usize = args
        .get(4)
        .ok_or("missing messages")?
        .parse()
        .map_err(|_| "bad messages")?;
    let slots_back: u32 = match args.get(5).map(String::as_str) {
        None => 0,
        Some("--prior-slot") if args.len() == 6 => 1,
        Some("--second-prior-slot") if args.len() == 6 => 2,
        _ => return Err("expected optional --prior-slot or --second-prior-slot".into()),
    };
    let (limit, key_offset) = if slots_back == 0 {
        (MAX_RECORDS, 0)
    } else {
        (
            MAX_PRIOR_SLOT_RECORDS,
            MAX_RECORDS + (slots_back as usize - 1) * MAX_PRIOR_SLOT_RECORDS,
        )
    };
    let leaf = CURRENT_LEAF - slots_back;
    if threads == 0 || messages != 1 || count > limit {
        return Err("invalid bounds".into());
    }
    let mut message = [0x42; 32];
    message[..8].copy_from_slice(&u64::from(slots_back).to_le_bytes());
    fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
    let next = AtomicUsize::new(0);
    let generated = AtomicUsize::new(0);
    let started = Instant::now();
    std::thread::scope(|scope| {
        let mut workers = Vec::new();
        for _ in 0..threads {
            workers.push(scope.spawn(|| -> Result<(), String> {
                loop {
                    let index = next.fetch_add(1, Ordering::Relaxed);
                    if index >= count {
                        break;
                    }
                    let path = directory.join(format!("key-{index:05}.bin"));
                    if path.exists() {
                        let bytes = fs::read(&path).map_err(|e| e.to_string())?;
                        if bytes.len() != messages * RECORD_BYTES {
                            return Err(format!("wrong fixture length: {}", path.display()));
                        }
                    } else {
                        // Public, deterministic BENCHMARK seeds; never use these for deposits.
                        let mut seed = [0x73; 32];
                        seed[..16].copy_from_slice(b"pq-geometry-2026");
                        seed[16..24].copy_from_slice(&((key_offset + index) as u64).to_le_bytes());
                        let bytes = fixture(&seed, &message, leaf)?;
                        let temporary = path.with_extension("tmp");
                        fs::write(&temporary, &bytes).map_err(|e| e.to_string())?;
                        fs::rename(&temporary, &path).map_err(|e| e.to_string())?;
                        let done = generated.fetch_add(1, Ordering::Relaxed) + 1;
                        if done % 32 == 0 {
                            eprintln!(
                                "Generated {done} new fixtures in {:.1}s",
                                started.elapsed().as_secs_f64()
                            );
                        }
                    }
                }
                Ok(())
            }));
        }
        for worker in workers {
            worker.join().map_err(|_| "fixture worker panicked")??;
        }
        Ok::<(), String>(())
    })?;
    let generated = generated.load(Ordering::Relaxed);
    let reused = count - generated;
    eprintln!(
        "Prepared {count} keys in {:.3}s ({generated} generated, {reused} cached)",
        started.elapsed().as_secs_f64()
    );
    Ok(())
}
