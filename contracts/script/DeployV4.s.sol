// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script, console2} from "forge-std/Script.sol";
import {AgentWorksEscrowV4} from "../src/AgentWorksEscrowV4.sol";
import {AgentWorksUmaArbiter} from "../src/AgentWorksUmaArbiter.sol";

/// @notice Deploys the v4 stack: a REAL decentralized arbiter (UMA Optimistic Oracle V3 adapter) wired
///         to live Sepolia UMA, then AgentWorksEscrowV4 pointed at that adapter. The arbiter is the ONLY
///         address that can rule disputes — no operator EOA. See docs/ARBITRATION.md.
/// @dev Env: DEPLOYER_PRIVATE_KEY, USDC_TOKEN_ADDRESS (escrow settlement token = MockUSDC), and optionally
///      UMA_OOV3_ADDRESS (default Sepolia OOv3), UMA_BOND_CURRENCY (default Sepolia UMA USDC), UMA_BOND
///      (default 400e6 = OOv3 minimum), UMA_LIVENESS (default 7200s), the escrow windows, and
///      SECONDS_PER_BLOCK (default 12 = Ethereum; set 2 for Base — bounds the resolve window vs liveness).
contract DeployV4 is Script {
    // UMA OOv3 on Ethereum Sepolia (verified): see docs.uma.xyz network addresses.
    address constant UMA_OOV3_SEPOLIA = 0xFd9e2642a170aDD10F53Ee14a93FcF2F31924944;
    address constant UMA_BOND_USDC_SEPOLIA = 0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238; // whitelisted

    function _u(string memory k, uint256 d) internal view returns (uint64) {
        return uint64(vm.envOr(k, d));
    }

    function run() external returns (AgentWorksEscrowV4 escrow, AgentWorksUmaArbiter arbiter) {
        vm.startBroadcast(vm.envUint("DEPLOYER_PRIVATE_KEY"));
        // 1. Arbiter first (the escrow ctor needs its address); deployer wires the escrow after.
        arbiter = new AgentWorksUmaArbiter(
            vm.envOr("UMA_OOV3_ADDRESS", UMA_OOV3_SEPOLIA),
            vm.envOr("UMA_BOND_CURRENCY", UMA_BOND_USDC_SEPOLIA),
            vm.envOr("UMA_BOND", uint256(400_000_000)), // 400 USDC (OOv3 minimum)
            _u("UMA_LIVENESS", 7200)
        );
        // 2. Escrow pointed at the arbiter.
        escrow = new AgentWorksEscrowV4(
            vm.envAddress("USDC_TOKEN_ADDRESS"),
            _u("REVEAL_DELAY_BLOCKS", 1),
            _u("REVEAL_WINDOW_BLOCKS", 256),
            _u("VOTING_WINDOW_BLOCKS", 600),
            _u("DISPUTE_WINDOW_BLOCKS", 50),
            // The resolve window must span the arbiter liveness in REAL SECONDS
            // (blocks × SECONDS_PER_BLOCK ≥ liveness) or the escrow ctor reverts ResolveWindowTooShort — so
            // resolveTimeout can never preempt an honest UMA optimistic ruling. On Ethereum (12s): 7200/12 = 600
            // blocks min (default 700 = comfortable margin). ON BASE set SECONDS_PER_BLOCK=2 and scale this to
            // ≥ liveness/2 (7200/2 = 3600 blocks); every other block-count window must be scaled for ~2s blocks too.
            _u("DISPUTE_RESOLVE_WINDOW_BLOCKS", 700),
            address(arbiter),
            _u("SECONDS_PER_BLOCK", 12) // deployment chain slot time: Ethereum ~12, Base ~2
        );
        // 3. One-shot wiring.
        arbiter.setEscrow(address(escrow));
        vm.stopBroadcast();

        console2.log("AgentWorksUmaArbiter:", address(arbiter));
        console2.log("AgentWorksEscrowV4:", address(escrow));
    }
}
