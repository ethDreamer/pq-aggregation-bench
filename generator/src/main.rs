use lean_xmss::internals::*;
use lean_xmss::{LeafIndex, LeanXmssPublicKey, LeanXmssSignature, verify};
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;
mod upstream_wots;

type Randomizer = [u8; RANDOMIZER_LEN];
const ZERO_DIGITS: usize = 6;
const MAX_RANDOMIZER_TRIALS: u64 = 1 << 32;
const TWEAK_PRF: u8 = 0;
const TWEAK_RANDOMIZER: u8 = 7;

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
    let mut signature = Vec::with_capacity(2404);
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
    let pk = LeanXmssPublicKey::from_bytes(&pk_bytes);
    let sig_array: [u8; 2404] = signature
        .as_slice()
        .try_into()
        .map_err(|_| "wrong signature size")?;
    let sig = LeanXmssSignature::from_bytes(&sig_array);
    if !verify(&pk, message, &sig) {
        return Err("pinned verifier rejected sparse signature".into());
    }
    let mut wrong_message = *message;
    wrong_message[0] ^= 1;
    if verify(&pk, &wrong_message, &sig) {
        return Err("wrong message accepted".into());
    }
    let mut result = Vec::with_capacity(2468);
    result.extend_from_slice(&pk_bytes);
    result.extend_from_slice(message);
    result.extend_from_slice(&signature);
    Ok(result)
}

fn main() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
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
    if threads == 0 || messages != 1 || count > 32768 {
        return Err("invalid bounds".into());
    }
    fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
    let next = AtomicUsize::new(0);
    let completed = AtomicUsize::new(0);
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
                        if bytes.len() != messages * 2468 {
                            return Err(format!("wrong fixture length: {}", path.display()));
                        }
                    } else {
                        // Public, deterministic BENCHMARK seeds; never use these for deposits.
                        let mut seed = [0x73; 32];
                        seed[..16].copy_from_slice(b"pq-geometry-2026");
                        seed[16..24].copy_from_slice(&(index as u64).to_le_bytes());
                        let mut bytes = Vec::with_capacity(messages * 2468);
                        for number in 0..messages {
                            let mut message = [0x42; 32];
                            message[..8].copy_from_slice(&(number as u64).to_le_bytes());
                            let leaf = 32768 + number as u32;
                            bytes.extend_from_slice(&fixture(&seed, &message, leaf)?);
                        }
                        let temporary = path.with_extension("tmp");
                        fs::write(&temporary, &bytes).map_err(|e| e.to_string())?;
                        fs::rename(&temporary, &path).map_err(|e| e.to_string())?;
                    }
                    let done = completed.fetch_add(1, Ordering::Relaxed) + 1;
                    if done % 32 == 0 || done == count {
                        eprintln!(
                            "fixtures {done}/{count}, elapsed {:.1}s",
                            started.elapsed().as_secs_f64()
                        );
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
    eprintln!(
        "complete: {count} keys × {messages} messages, {:.3}s",
        started.elapsed().as_secs_f64()
    );
    Ok(())
}
