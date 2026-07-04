// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {AgentWorksEscrowV4, IERC20, IArbiter} from "../src/AgentWorksEscrowV4.sol";
import {MockUSDC} from "../src/MockUSDC.sol";

/// @dev Records openDispute calls and can relay a ruling back, standing in for a real IArbiter adapter.
contract MockArbiter is IArbiter {
    uint256 public lastJobId;
    bool public lastCommitteePayout;
    address public lastDisputer;
    bool public opened;

    function openDispute(uint256 jobId, bool committeePayout, address disputer) external override {
        lastJobId = jobId;
        lastCommitteePayout = committeePayout;
        lastDisputer = disputer;
        opened = true;
    }

    function rule(address escrow, uint256 jobId, bool payProvider) external {
        AgentWorksEscrowV4(escrow).resolveDispute(jobId, payProvider);
    }
}

/// @dev An arbiter that exposes an optimistic liveness (like the UMA adapter), used to exercise the
///      escrow's constructor coupling of the resolve window to the arbiter liveness (audit: resolveTimeout).
contract MockArbiterWithLiveness is IArbiter {
    uint64 public liveness;

    constructor(uint64 liveness_) {
        liveness = liveness_;
    }

    function openDispute(uint256, bool, address) external override {}
}

contract AgentWorksEscrowV4Test is Test {
    AgentWorksEscrowV4 internal escrow;
    MockUSDC internal usdc;
    MockArbiter internal arbiter;

    address internal client = makeAddr("client");
    address internal provider = makeAddr("provider");
    address internal provider2 = makeAddr("provider2");
    address internal ev1 = makeAddr("ev1");
    address internal ev2 = makeAddr("ev2");
    address internal ev3 = makeAddr("ev3");
    address internal stranger = makeAddr("stranger");

    uint256 internal constant AMOUNT = 100_000_000; // 100 USDC
    bytes32 internal constant SPEC = keccak256("spec: write a haiku about escrow");
    bytes32 internal constant DELIVERABLE = keccak256("irys://deliverable-content-id");
    string internal constant IRYS_ID = "kS5LZ8nT9example_irys_data_item_id_43charsAB";
    bytes32 internal constant SALT = keccak256("provider-secret-salt");
    bytes32 internal constant SALT2 = keccak256("provider2-secret-salt");

    uint64 internal constant DELAY = 1;
    uint64 internal constant RWIN = 50;   // reveal window
    uint64 internal constant VWIN = 50;   // voting window
    uint64 internal constant DWIN = 30;   // dispute window
    uint64 internal constant RSWIN = 50;  // dispute-resolve window
    uint8  internal constant QUORUM = 2;  // 2-of-3

    uint64 internal deadline;

    event JobCreated(uint256 indexed jobId, address indexed client, uint256 amount, bytes32 specHash, uint64 deadline, uint8 committeeSize, uint8 quorum);
    event JobFunded(uint256 indexed jobId, uint256 amount);
    event JobAccepted(uint256 indexed jobId, address indexed provider);
    event WorkSubmitted(uint256 indexed jobId, bytes32 deliverableHash, string irysId);
    event VoteCast(uint256 indexed jobId, address indexed member, bool approve, uint8 newCount);
    event JobResolved(uint256 indexed jobId, bool tentativePayout, uint8 approveCount, uint8 rejectCount);
    event JobDisputed(uint256 indexed jobId, address indexed disputer, bool committeePayout);
    event DisputeResolved(uint256 indexed jobId, bool payProvider);
    event DisputeTimedOut(uint256 indexed jobId, bool tentativePayout);
    event JobCompleted(uint256 indexed jobId, address indexed provider, uint256 amount);
    event JobRejected(uint256 indexed jobId, address indexed client, uint256 amount);
    event RefundClaimed(uint256 indexed jobId, address indexed client, uint256 amount);

    function setUp() public {
        usdc = new MockUSDC();
        arbiter = new MockArbiter();
        escrow = new AgentWorksEscrowV4(address(usdc), DELAY, RWIN, VWIN, DWIN, RSWIN, address(arbiter));
        deadline = uint64(block.timestamp + 7 days);
        usdc.mint(client, 1_000_000_000);
        vm.prank(client);
        usdc.approve(address(escrow), type(uint256).max);
    }

    // ── helpers ──

    function _committee() internal view returns (address[] memory c) {
        c = new address[](3);
        c[0] = ev1; c[1] = ev2; c[2] = ev3;
    }

    function _commitment(uint256 jobId, address who, bytes32 salt) internal pure returns (bytes32) {
        return keccak256(abi.encode(jobId, who, salt));
    }

    function _create() internal returns (uint256 jobId) {
        vm.prank(client);
        jobId = escrow.createJob(_committee(), QUORUM, AMOUNT, SPEC, deadline);
    }

    function _createAndFund() internal returns (uint256 jobId) {
        jobId = _create();
        vm.prank(client);
        escrow.fund(jobId);
    }

    function _createFundCommitReveal() internal returns (uint256 jobId) {
        jobId = _createAndFund();
        vm.prank(provider);
        escrow.commitAccept(_commitment(jobId, provider, SALT));
        vm.roll(block.number + DELAY);
        vm.prank(provider);
        escrow.revealAccept(jobId, SALT);
    }

    function _createFundAcceptSubmit() internal returns (uint256 jobId) {
        jobId = _createFundCommitReveal();
        vm.prank(provider);
        escrow.submitWork(jobId, DELIVERABLE, IRYS_ID);
    }

    function _resolve(uint256 jobId, bool approve) internal {
        vm.prank(ev1);
        escrow.castVote(jobId, approve);
        vm.prank(ev2);
        escrow.castVote(jobId, approve); // 2nd vote reaches quorum → Resolved
    }

    // ── constructor ──

    function test_constructor_setsImmutables() public view {
        assertEq(address(escrow.token()), address(usdc));
        assertEq(escrow.revealDelayBlocks(), DELAY);
        assertEq(escrow.votingWindowBlocks(), VWIN);
        assertEq(escrow.disputeWindowBlocks(), DWIN);
        assertEq(escrow.disputeResolveWindowBlocks(), RSWIN);
        assertEq(escrow.arbiter(), address(arbiter));
    }

    function test_constructor_revertsZeroToken() public {
        vm.expectRevert(AgentWorksEscrowV4.ZeroAddress.selector);
        new AgentWorksEscrowV4(address(0), DELAY, RWIN, VWIN, DWIN, RSWIN, address(arbiter));
    }

    function test_constructor_revertsZeroArbiter() public {
        vm.expectRevert(AgentWorksEscrowV4.ZeroAddress.selector);
        new AgentWorksEscrowV4(address(usdc), DELAY, RWIN, VWIN, DWIN, RSWIN, address(0));
    }

    function test_constructor_revertsZeroVotingWindow() public {
        vm.expectRevert(AgentWorksEscrowV4.InvalidWindow.selector);
        new AgentWorksEscrowV4(address(usdc), DELAY, RWIN, 0, DWIN, RSWIN, address(arbiter));
    }

    function test_constructor_revertsZeroDisputeWindow() public {
        vm.expectRevert(AgentWorksEscrowV4.InvalidWindow.selector);
        new AgentWorksEscrowV4(address(usdc), DELAY, RWIN, VWIN, 0, RSWIN, address(arbiter));
    }

    function test_constructor_revertsZeroResolveWindow() public {
        vm.expectRevert(AgentWorksEscrowV4.InvalidWindow.selector);
        new AgentWorksEscrowV4(address(usdc), DELAY, RWIN, VWIN, DWIN, 0, address(arbiter));
    }

    // ── resolve-window ↔ arbiter-liveness coupling (audit: resolveTimeout preemption) ──

    // liveness 7200s needs ≥ 600 blocks (×12s/block); 599 must revert so the timeout can't preempt UMA.
    function test_constructor_revertsResolveWindowShorterThanLiveness() public {
        MockArbiterWithLiveness a = new MockArbiterWithLiveness(7200);
        vm.expectRevert(AgentWorksEscrowV4.ResolveWindowTooShort.selector);
        new AgentWorksEscrowV4(address(usdc), DELAY, RWIN, VWIN, DWIN, 599, address(a));
    }

    function test_constructor_okWhenResolveWindowCoversLiveness() public {
        MockArbiterWithLiveness a = new MockArbiterWithLiveness(7200);
        AgentWorksEscrowV4 e = new AgentWorksEscrowV4(address(usdc), DELAY, RWIN, VWIN, DWIN, 600, address(a));
        assertEq(e.disputeResolveWindowBlocks(), 600); // 600×12 == 7200, exactly covers liveness
    }

    // An arbiter without a liveness() (synchronous ruling / the default MockArbiter) skips the coupling —
    // the setUp escrow deployed with RSWIN=50 < 600 and did NOT revert.
    function test_constructor_skipsCouplingWhenArbiterHasNoLiveness() public view {
        assertEq(escrow.disputeResolveWindowBlocks(), RSWIN);
    }

    // ── createJob (committee) ──

    function test_createJob_setsCommitteeAndEmits() public {
        vm.expectEmit(true, true, false, true);
        emit JobCreated(1, client, AMOUNT, SPEC, deadline, 3, QUORUM);
        uint256 jobId = _create();

        AgentWorksEscrowV4.Job memory job = escrow.getJob(jobId);
        assertEq(job.client, client);
        assertEq(job.provider, address(0));
        assertEq(job.committeeSize, 3);
        assertEq(job.quorum, QUORUM);
        assertEq(uint8(job.status), uint8(AgentWorksEscrowV4.Status.Open));
        address[] memory c = escrow.getCommittee(jobId);
        assertEq(c.length, 3);
        assertEq(c[0], ev1);
        assertEq(c[2], ev3);
    }

    function test_createJob_revertsEvenCommittee() public {
        address[] memory c = new address[](2);
        c[0] = ev1; c[1] = ev2;
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.CommitteeNotOdd.selector, 2));
        escrow.createJob(c, 2, AMOUNT, SPEC, deadline);
    }

    function test_createJob_revertsTooLarge() public {
        address[] memory c = new address[](9);
        for (uint256 i; i < 9; ++i) c[i] = makeAddr(string(abi.encodePacked("e", i)));
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadCommitteeSize.selector, 9));
        escrow.createJob(c, 5, AMOUNT, SPEC, deadline);
    }

    function test_createJob_revertsEmptyCommittee() public {
        address[] memory c = new address[](0);
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadCommitteeSize.selector, 0));
        escrow.createJob(c, 1, AMOUNT, SPEC, deadline);
    }

    function test_createJob_revertsDuplicateEvaluator() public {
        address[] memory c = new address[](3);
        c[0] = ev1; c[1] = ev1; c[2] = ev3;
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.DuplicateEvaluator.selector, ev1));
        escrow.createJob(c, 2, AMOUNT, SPEC, deadline);
    }

    function test_createJob_revertsEvaluatorIsClient() public {
        address[] memory c = new address[](3);
        c[0] = ev1; c[1] = client; c[2] = ev3;
        vm.prank(client);
        vm.expectRevert(AgentWorksEscrowV4.EvaluatorIsClient.selector);
        escrow.createJob(c, 2, AMOUNT, SPEC, deadline);
    }

    function test_createJob_revertsQuorumBelowMajority() public {
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadQuorum.selector, 1, 2, 3));
        escrow.createJob(_committee(), 1, AMOUNT, SPEC, deadline);
    }

    function test_createJob_revertsQuorumAboveN() public {
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadQuorum.selector, 4, 2, 3));
        escrow.createJob(_committee(), 4, AMOUNT, SPEC, deadline);
    }

    function test_createJob_acceptsUnanimousQuorum() public {
        vm.prank(client);
        uint256 jobId = escrow.createJob(_committee(), 3, AMOUNT, SPEC, deadline);
        assertEq(escrow.getJob(jobId).quorum, 3);
    }

    function test_createJob_revertsZeroAmount() public {
        vm.prank(client);
        vm.expectRevert(AgentWorksEscrowV4.ZeroAmount.selector);
        escrow.createJob(_committee(), 2, 0, SPEC, deadline);
    }

    function test_createJob_revertsPastDeadline() public {
        vm.prank(client);
        vm.expectRevert(AgentWorksEscrowV4.InvalidDeadline.selector);
        escrow.createJob(_committee(), 2, AMOUNT, SPEC, uint64(block.timestamp));
    }

    // ── fund ──

    function test_fund_escrowsUSDC() public {
        uint256 jobId = _create();
        vm.prank(client);
        escrow.fund(jobId);
        assertEq(usdc.balanceOf(address(escrow)), AMOUNT);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Funded));
    }

    function test_fund_revertsIfNotClient() public {
        uint256 jobId = _create();
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.NotClient.selector, stranger));
        escrow.fund(jobId);
    }

    // ── sealed accept (v3 carried — key cases) ──

    function test_revealAccept_setsProviderAndStatus() public {
        uint256 jobId = _createFundCommitReveal();
        AgentWorksEscrowV4.Job memory job = escrow.getJob(jobId);
        assertEq(job.provider, provider);
        assertEq(uint8(job.status), uint8(AgentWorksEscrowV4.Status.Accepted));
    }

    function test_revealAccept_revertsIfCommitteeMember() public {
        uint256 jobId = _createAndFund();
        vm.prank(ev1);
        escrow.commitAccept(_commitment(jobId, ev1, SALT));
        vm.roll(block.number + DELAY);
        vm.prank(ev1);
        vm.expectRevert(AgentWorksEscrowV4.ProviderIsCommitteeMember.selector);
        escrow.revealAccept(jobId, SALT);
    }

    function test_revealAccept_revertsIfClient() public {
        uint256 jobId = _createAndFund();
        vm.prank(client);
        escrow.commitAccept(_commitment(jobId, client, SALT));
        vm.roll(block.number + DELAY);
        vm.prank(client);
        vm.expectRevert(AgentWorksEscrowV4.ProviderIsClient.selector);
        escrow.revealAccept(jobId, SALT);
    }

    function test_revealAccept_copiedCommitmentByOtherSenderFails() public {
        uint256 jobId = _createAndFund();
        bytes32 victim = _commitment(jobId, provider, SALT);
        vm.prank(provider2);
        escrow.commitAccept(victim);
        vm.roll(block.number + DELAY);
        vm.prank(provider2);
        vm.expectRevert(AgentWorksEscrowV4.CommitNotFound.selector);
        escrow.revealAccept(jobId, SALT);
    }

    function test_revealAccept_revertsBeforeDelay() public {
        uint256 jobId = _createAndFund();
        uint64 ready = uint64(block.number) + DELAY;
        vm.prank(provider);
        escrow.commitAccept(_commitment(jobId, provider, SALT));
        vm.prank(provider);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.TooEarlyToReveal.selector, ready));
        escrow.revealAccept(jobId, SALT);
    }

    function test_revealAccept_twoValidCommits_firstWins() public {
        uint256 jobId = _createAndFund();
        vm.prank(provider);
        escrow.commitAccept(_commitment(jobId, provider, SALT));
        vm.prank(provider2);
        escrow.commitAccept(_commitment(jobId, provider2, SALT2));
        vm.roll(block.number + DELAY);
        vm.prank(provider);
        escrow.revealAccept(jobId, SALT);
        vm.prank(provider2);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Accepted, AgentWorksEscrowV4.Status.Funded));
        escrow.revealAccept(jobId, SALT2);
        assertEq(escrow.getJob(jobId).provider, provider);
    }

    // ── submitWork (+ arms voting) ──

    function test_submitWork_armsVotingDeadline() public {
        uint256 jobId = _createFundCommitReveal();
        uint256 atBlock = block.number;
        vm.prank(provider);
        escrow.submitWork(jobId, DELIVERABLE, IRYS_ID);
        (, , uint64 votingDeadline, , ) = escrow.getVote(jobId);
        assertEq(votingDeadline, uint64(atBlock) + VWIN);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Submitted));
    }

    function test_submitWork_revertsIfNotProvider() public {
        uint256 jobId = _createFundCommitReveal();
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.NotProvider.selector, stranger));
        escrow.submitWork(jobId, DELIVERABLE, IRYS_ID);
    }

    // ── castVote ──

    function test_castVote_memberApproveIncrements() public {
        uint256 jobId = _createFundAcceptSubmit();
        vm.expectEmit(true, true, false, true);
        emit VoteCast(jobId, ev1, true, 1);
        vm.prank(ev1);
        escrow.castVote(jobId, true);
        (uint8 a, uint8 r, , , ) = escrow.getVote(jobId);
        assertEq(a, 1);
        assertEq(r, 0);
        assertTrue(escrow.hasMemberVoted(jobId, ev1));
    }

    function test_castVote_revertsNonMember() public {
        uint256 jobId = _createFundAcceptSubmit();
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.NotCommitteeMember.selector, stranger));
        escrow.castVote(jobId, true);
    }

    function test_castVote_revertsDoubleVote() public {
        uint256 jobId = _createFundAcceptSubmit();
        vm.prank(ev1);
        escrow.castVote(jobId, true);
        vm.prank(ev1);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.AlreadyVoted.selector, ev1));
        escrow.castVote(jobId, true);
    }

    function test_castVote_revertsIfNotSubmitted() public {
        uint256 jobId = _createFundCommitReveal(); // Accepted, not Submitted
        vm.prank(ev1);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Accepted, AgentWorksEscrowV4.Status.Submitted));
        escrow.castVote(jobId, true);
    }

    function test_castVote_revertsAfterVotingDeadline() public {
        uint256 jobId = _createFundAcceptSubmit();
        (, , uint64 dl, , ) = escrow.getVote(jobId);
        vm.roll(uint256(dl) + 1);
        vm.prank(ev1);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.VotingClosed.selector, dl));
        escrow.castVote(jobId, true);
    }

    function test_castVote_quorumApprove_resolvesPayout_noFundsMove() public {
        uint256 jobId = _createFundAcceptSubmit();
        uint256 escrowBalBefore = usdc.balanceOf(address(escrow));
        vm.prank(ev1);
        escrow.castVote(jobId, true);
        // the quorum-reaching vote emits VoteCast then JobResolved — assert the resolve event in order
        vm.expectEmit(true, true, false, true);
        emit VoteCast(jobId, ev2, true, 2);
        vm.expectEmit(true, false, false, true);
        emit JobResolved(jobId, true, 2, 0);
        vm.prank(ev2);
        escrow.castVote(jobId, true);
        (, , , bool tentativePayout, ) = escrow.getVote(jobId);
        assertTrue(tentativePayout);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Resolved));
        assertEq(usdc.balanceOf(address(escrow)), escrowBalBefore); // NO funds moved
    }

    function test_castVote_quorumReject_resolvesRefund_noFundsMove() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, false);
        (, , , bool tentativePayout, ) = escrow.getVote(jobId);
        assertFalse(tentativePayout);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Resolved));
        assertEq(usdc.balanceOf(address(escrow)), AMOUNT);
    }

    function test_castVote_afterResolve_reverts() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true); // Resolved
        vm.prank(ev3);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Resolved, AgentWorksEscrowV4.Status.Submitted));
        escrow.castVote(jobId, true);
    }

    function test_castVote_splitNoQuorum_staysSubmitted() public {
        uint256 jobId = _createFundAcceptSubmit();
        vm.prank(ev1);
        escrow.castVote(jobId, true);
        vm.prank(ev2);
        escrow.castVote(jobId, false); // 1-1, no quorum
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Submitted));
    }

    // ── forceResolve ──

    function test_forceResolve_afterDeadline_refunds() public {
        uint256 jobId = _createFundAcceptSubmit();
        vm.prank(ev1);
        escrow.castVote(jobId, true); // 1 approve only
        (, , uint64 dl, , ) = escrow.getVote(jobId);
        vm.roll(uint256(dl) + 1);
        escrow.forceResolve(jobId);
        (, , , bool tentativePayout, ) = escrow.getVote(jobId);
        assertFalse(tentativePayout);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Resolved));
    }

    function test_forceResolve_revertsBeforeDeadline() public {
        uint256 jobId = _createFundAcceptSubmit();
        (, , uint64 dl, , ) = escrow.getVote(jobId);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.VotingStillOpen.selector, dl));
        escrow.forceResolve(jobId);
    }

    // ── finalize ──

    function test_finalize_payout_paysProviderAfterWindow() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        (, , , , uint64 resolvedBlock) = escrow.getVote(jobId);
        vm.roll(uint256(resolvedBlock) + DWIN + 1);
        uint256 provBefore = usdc.balanceOf(provider);
        vm.expectEmit(true, true, false, true);
        emit JobCompleted(jobId, provider, AMOUNT);
        escrow.finalize(jobId);
        assertEq(usdc.balanceOf(provider), provBefore + AMOUNT);
        assertEq(usdc.balanceOf(address(escrow)), 0);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Completed));
    }

    function test_finalize_refund_refundsClientAfterWindow() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, false);
        (, , , , uint64 resolvedBlock) = escrow.getVote(jobId);
        vm.roll(uint256(resolvedBlock) + DWIN + 1);
        uint256 clientBefore = usdc.balanceOf(client);
        escrow.finalize(jobId);
        assertEq(usdc.balanceOf(client), clientBefore + AMOUNT);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Rejected));
    }

    function test_finalize_revertsDuringWindow() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        (, , , , uint64 resolvedBlock) = escrow.getVote(jobId);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.DisputeWindowOpen.selector, resolvedBlock + DWIN));
        escrow.finalize(jobId);
    }

    function test_finalize_anyoneCanCall() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        (, , , , uint64 resolvedBlock) = escrow.getVote(jobId);
        vm.roll(uint256(resolvedBlock) + DWIN + 1);
        vm.prank(stranger);
        escrow.finalize(jobId);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Completed));
    }

    function test_finalize_revertsIfNotResolved() public {
        uint256 jobId = _createFundAcceptSubmit();
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Submitted, AgentWorksEscrowV4.Status.Resolved));
        escrow.finalize(jobId);
    }

    // ── dispute ──

    function test_dispute_loserBondsAndEscalates() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true); // payout tentative → loser = client
        vm.expectEmit(true, true, false, true);
        emit JobDisputed(jobId, client, true);
        vm.prank(client);
        escrow.dispute(jobId);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Disputed));
        assertTrue(arbiter.opened());
        assertEq(arbiter.lastJobId(), jobId);
        assertEq(arbiter.lastDisputer(), client);
        (address disputer, ) = escrow.getDispute(jobId);
        assertEq(disputer, client);
    }

    function test_dispute_providerDisputesRefund() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, false); // refund tentative → loser = provider
        vm.prank(provider);
        escrow.dispute(jobId);
        assertEq(arbiter.lastDisputer(), provider);
    }

    function test_dispute_revertsIfWinnerTries() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true); // payout → provider is the winner
        vm.prank(provider);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.NotLosingParty.selector, provider));
        escrow.dispute(jobId);
    }

    function test_dispute_revertsByStranger() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.NotLosingParty.selector, stranger));
        escrow.dispute(jobId);
    }

    function test_dispute_revertsAfterWindowClosed() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        (, , , , uint64 resolvedBlock) = escrow.getVote(jobId);
        vm.roll(uint256(resolvedBlock) + DWIN + 1);
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.DisputeWindowClosed.selector, resolvedBlock + DWIN));
        escrow.dispute(jobId);
    }

    function test_dispute_revertsIfNotResolved() public {
        uint256 jobId = _createFundAcceptSubmit();
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Submitted, AgentWorksEscrowV4.Status.Resolved));
        escrow.dispute(jobId);
    }

    // ── resolveDispute (arbiter only) ──

    function test_resolveDispute_arbiterOverturnsRefund_paysProvider() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, false); // refund tentative; provider disputes
        vm.prank(provider);
        escrow.dispute(jobId);
        uint256 provBefore = usdc.balanceOf(provider);
        vm.expectEmit(true, false, false, true);
        emit DisputeResolved(jobId, true);
        arbiter.rule(address(escrow), jobId, true); // arbiter rules pay provider
        assertEq(usdc.balanceOf(provider), provBefore + AMOUNT);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Completed));
    }

    function test_resolveDispute_arbiterUpholdsPayout() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true); // payout tentative; client disputes
        vm.prank(client);
        escrow.dispute(jobId);
        arbiter.rule(address(escrow), jobId, true); // uphold payout
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Completed));
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    function test_resolveDispute_revertsNonArbiter() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        vm.prank(client);
        escrow.dispute(jobId);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.NotArbiter.selector, stranger));
        escrow.resolveDispute(jobId, true);
    }

    function test_resolveDispute_revertsIfNotDisputed() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true); // Resolved, never disputed
        vm.prank(address(arbiter));
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Resolved, AgentWorksEscrowV4.Status.Disputed));
        escrow.resolveDispute(jobId, true);
    }

    // ── resolveTimeout (anti-freeze) ──

    function test_resolveTimeout_executesTentativeAfterDeadline() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true); // payout tentative
        vm.prank(client);
        escrow.dispute(jobId);
        (, uint64 disputeBlock) = escrow.getDispute(jobId);
        vm.roll(uint256(disputeBlock) + RSWIN + 1);
        uint256 provBefore = usdc.balanceOf(provider);
        vm.expectEmit(true, false, false, true);
        emit DisputeTimedOut(jobId, true);
        vm.prank(stranger); // permissionless
        escrow.resolveTimeout(jobId);
        assertEq(usdc.balanceOf(provider), provBefore + AMOUNT);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Completed));
    }

    function test_resolveTimeout_revertsBeforeDeadline() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        vm.prank(client);
        escrow.dispute(jobId);
        (, uint64 disputeBlock) = escrow.getDispute(jobId);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.ResolveWindowOpen.selector, disputeBlock + RSWIN));
        escrow.resolveTimeout(jobId);
    }

    function test_resolveTimeout_revertsIfNotDisputed() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Resolved, AgentWorksEscrowV4.Status.Disputed));
        escrow.resolveTimeout(jobId);
    }

    // ── claimRefund (backstop + new-status exclusions) ──

    function test_claimRefund_afterDeadline_whenFundedUnaccepted() public {
        uint256 jobId = _createAndFund(); // Funded, no provider ever revealed
        uint256 clientBefore = usdc.balanceOf(client);
        vm.warp(deadline + 1);
        vm.prank(client);
        escrow.claimRefund(jobId);
        assertEq(usdc.balanceOf(client), clientBefore + AMOUNT);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Refunded));
    }

    function test_claimRefund_afterDeadline_whenAcceptedNoWork() public {
        uint256 jobId = _createFundCommitReveal(); // Accepted, provider never submitted
        uint256 clientBefore = usdc.balanceOf(client);
        vm.warp(deadline + 1);
        vm.prank(client);
        escrow.claimRefund(jobId);
        assertEq(usdc.balanceOf(client), clientBefore + AMOUNT);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Refunded));
    }

    // SECURITY (audit #4): once work is Submitted the client can NOT unilaterally refund and keep the
    // delivered work — even past a self-chosen short deadline. The committee owns the outcome.
    function test_claimRefund_revertsWhenSubmitted_cannotStealWork() public {
        uint256 jobId = _createFundAcceptSubmit();
        vm.warp(deadline + 1); // deadline expired mid-voting
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Submitted, AgentWorksEscrowV4.Status.Funded));
        escrow.claimRefund(jobId);
    }

    // ...and a stalled Submitted job is still flushable permissionlessly (forceResolve → refund), so
    // removing Submitted from claimRefund never strands funds.
    function test_submittedStall_flushedByForceResolve_notClaimRefund() public {
        uint256 jobId = _createFundAcceptSubmit();
        vm.roll(block.number + VWIN + 1); // voting window elapsed with no quorum
        uint256 clientBefore = usdc.balanceOf(client);
        vm.prank(stranger); // permissionless
        escrow.forceResolve(jobId); // → Resolved (tentative refund)
        (, , , , uint64 resolvedBlock) = escrow.getVote(jobId);
        vm.roll(uint256(resolvedBlock) + DWIN + 1);
        escrow.finalize(jobId); // dispute window elapsed → refund executes
        assertEq(usdc.balanceOf(client), clientBefore + AMOUNT);
        assertEq(uint8(escrow.getJob(jobId).status), uint8(AgentWorksEscrowV4.Status.Rejected));
    }

    function test_claimRefund_revertsWhenResolved() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        vm.warp(deadline + 1);
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Resolved, AgentWorksEscrowV4.Status.Funded));
        escrow.claimRefund(jobId);
    }

    function test_claimRefund_revertsWhenDisputed() public {
        uint256 jobId = _createFundAcceptSubmit();
        _resolve(jobId, true);
        vm.prank(client);
        escrow.dispute(jobId);
        vm.warp(deadline + 1);
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.BadStatus.selector, AgentWorksEscrowV4.Status.Disputed, AgentWorksEscrowV4.Status.Funded));
        escrow.claimRefund(jobId);
    }

    function test_claimRefund_revertsBeforeDeadline() public {
        uint256 jobId = _createAndFund();
        vm.prank(client);
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.DeadlineNotReached.selector, deadline));
        escrow.claimRefund(jobId);
    }

    // ── views / misc ──

    function test_getJob_revertsForUnknown() public {
        vm.expectRevert(abi.encodeWithSelector(AgentWorksEscrowV4.JobNotFound.selector, uint256(999)));
        escrow.getJob(999);
    }

    function test_twoJobs_independentIds() public {
        assertEq(_create(), 1);
        assertEq(_create(), 2);
    }

    // ── CEI reentrancy on finalize ──

    function test_finalizeIsCEISafe() public {
        ReentrantToken evil = new ReentrantToken();
        MockArbiter arb = new MockArbiter();
        AgentWorksEscrowV4 evilEscrow = new AgentWorksEscrowV4(address(evil), DELAY, RWIN, VWIN, DWIN, RSWIN, address(arb));
        evil.mint(client, AMOUNT);
        vm.prank(client);
        evil.approve(address(evilEscrow), type(uint256).max);

        vm.prank(client);
        uint256 jobId = evilEscrow.createJob(_committee(), QUORUM, AMOUNT, SPEC, deadline);
        vm.prank(client);
        evilEscrow.fund(jobId);
        vm.prank(provider);
        evilEscrow.commitAccept(_commitment(jobId, provider, SALT));
        vm.roll(block.number + DELAY);
        vm.prank(provider);
        evilEscrow.revealAccept(jobId, SALT);
        vm.prank(provider);
        evilEscrow.submitWork(jobId, DELIVERABLE, IRYS_ID);
        vm.prank(ev1);
        evilEscrow.castVote(jobId, true);
        vm.prank(ev2);
        evilEscrow.castVote(jobId, true);
        (, , , , uint64 resolvedBlock) = evilEscrow.getVote(jobId);
        vm.roll(uint256(resolvedBlock) + DWIN + 1);

        evil.arm(address(evilEscrow), jobId);
        vm.expectRevert(); // reentrant finalize reverts (status already Completed)
        evilEscrow.finalize(jobId);
    }
}

/// @dev ERC-20 that reenters escrow.finalize() on transfer, to prove CEI ordering holds.
contract ReentrantToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    address public escrow;
    uint256 public armedJobId;
    bool internal entered;

    function arm(address e, uint256 jobId) external { escrow = e; armedJobId = jobId; }
    function mint(address to, uint256 amt) external { balanceOf[to] += amt; }
    function approve(address s, uint256 a) external returns (bool) { allowance[msg.sender][s] = a; return true; }

    function transferFrom(address f, address t, uint256 a) external returns (bool) {
        balanceOf[f] -= a; balanceOf[t] += a; return true;
    }

    function transfer(address t, uint256 a) external returns (bool) {
        if (armedJobId != 0 && !entered) {
            entered = true;
            AgentWorksEscrowV4(escrow).finalize(armedJobId);
        }
        balanceOf[msg.sender] -= a; balanceOf[t] += a; return true;
    }
}
