// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// @notice Minimal ERC-20 interface (the subset of USDC the escrow uses).
interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @notice The pluggable arbiter seam. The escrow is decoupled from HOW a dispute is judged: it only
///         knows an `arbiter` address implementing this, and only accepts {AgentWorksEscrowV4.resolveDispute}
///         back from that address. A real decentralized adapter (e.g. the UMA Optimistic Oracle V3
///         adapter `AgentWorksUmaArbiter`, or a Kleros ERC-792 wrapper) occupies the arbiter slot, so
///         NO operator EOA can rule. Swapping adapters is a deploy-time change with zero escrow changes.
interface IArbiter {
    /// @param jobId the disputed job
    /// @param committeePayout the committee's tentative outcome (true = pay provider)
    /// @param disputer the losing party who escalated (and who stakes the bond at the adapter)
    function openDispute(uint256 jobId, bool committeePayout, address disputer) external;
}

/// @notice Optional arbiter extension. Adapters whose ruling is OPTIMISTIC (settle only after a liveness
///         period — e.g. the UMA Optimistic Oracle V3 adapter) expose that liveness so the escrow can
///         guarantee, at construction, that its resolve-timeout window can never preempt a live ruling.
///         Adapters that rule synchronously simply omit it (the escrow's ctor check is skipped).
interface IArbiterLiveness {
    function liveness() external view returns (uint64);
}

/// @title AgentWorksEscrowV4
/// @notice Open-marketplace settlement layer hardened against a single point of failure in EVALUATION.
///         v3's lone `evaluator` (who could hallucinate, go offline, or be compromised and unilaterally
///         drain/refund) is replaced by:
///           1. An M-of-N evaluator COMMITTEE (odd N, strict-majority quorum) that each pull the
///              deliverable from Irys, judge it, and {castVote}. Reaching quorum produces a TENTATIVE
///              outcome — NO funds move yet.
///           2. A STAKED DISPUTE window: the losing side may escalate by staking a bond at a decoupled,
///              decentralized arbiter ({IArbiter}; live adapter = UMA Optimistic Oracle V3). The arbiter's
///              ruling is final. If nobody disputes, anyone {finalize}s the tentative outcome.
///         The entire v3 sealed commit-reveal accept (anti-frontrunning) is carried over verbatim.
/// @dev    New immutable contract (mirrors v1→v2→v3 history); v3 stays deployed. Lifecycle:
///         createJob(committee) → fund → commitAccept → revealAccept → submitWork → castVote ×N →
///         Resolved(tentative) → finalize | dispute → (Disputed) resolveDispute | resolveTimeout.
///         Funds move only in finalize/resolveDispute/resolveTimeout/claimRefund — all CEI-ordered.
///         `resolveDispute` is arbiter-only (no admin key); `resolveTimeout` only ever executes the
///         committee's own tentative outcome (an anti-freeze backstop, never an arbitrary ruling).
contract AgentWorksEscrowV4 {
    enum Status {
        None,       // 0: unset / job does not exist
        Open,       // 1: created, awaiting funding (no provider yet)
        Funded,     // 2: USDC escrowed, open for any provider to commit + reveal
        Accepted,   // 3: a provider revealed and claimed the job, awaiting work
        Submitted,  // 4: deliverable submitted, committee voting window OPEN
        Resolved,   // 5: committee reached a TENTATIVE outcome; dispute window OPEN, funds NOT moved
        Disputed,   // 6: losing side staked a bond + escalated; awaiting arbiter ruling
        Completed,  // 7: terminal — provider paid
        Rejected,   // 8: terminal — client refunded
        Refunded    // 9: terminal — client reclaimed after deadline backstop
    }

    struct Job {
        address client;          // funds the job; can reclaim after deadline
        address provider;        // zero until revealAccept; performs the work, paid on completion
        uint256 amount;          // USDC base units (6 decimals)
        bytes32 specHash;        // hash of the task specification
        bytes32 deliverableHash; // keccak256 of the deliverable content, set on submitWork
        string  irysId;          // Irys data-item id where the deliverable is stored
        uint64  deadline;        // unix seconds; after this an unresolved funded job is refundable
        Status  status;
        uint8   committeeSize;   // N evaluators (odd, ≤ MAX_COMMITTEE)
        uint8   quorum;          // votes needed for either side (strict majority ≤ quorum ≤ N)
    }

    /// @notice A sealed accept bid (v3, carried verbatim). Keyed by the commitment hash.
    struct Commit {
        uint64 commitBlock; // block.number at commitAccept; 0 == no live commitment
        bool   revealed;    // set once on a successful reveal (one-shot anti-replay)
    }

    /// @notice Per-job committee voting state.
    struct Vote {
        uint8  approveCount;
        uint8  rejectCount;
        uint64 votingDeadlineBlock; // armed at submitWork: block.number + votingWindowBlocks
        bool   tentativePayout;     // meaningful once Resolved: true = pay provider, false = refund
        uint64 resolvedBlock;       // block Resolved was entered (dispute-window anchor)
    }

    /// @notice Per-job dispute state. The stake itself is held at the arbiter adapter (the UMA bond),
    ///         not in the escrow; this records who escalated + when (resolve-timeout anchor).
    struct DisputeRec {
        address disputer;
        uint64  disputeBlock;
    }

    /// @notice The settlement token (USDC, or MockUSDC for tests). Immutable.
    IERC20 public immutable token;

    // ── v3 sealed-accept timing (carried) ──
    uint64 public immutable revealDelayBlocks;
    uint64 public immutable revealWindowBlocks;
    // ── v4 settlement timing ──
    uint64 public immutable votingWindowBlocks;         // blocks after submitWork to reach quorum
    uint64 public immutable disputeWindowBlocks;        // blocks after Resolved to raise a dispute
    uint64 public immutable disputeResolveWindowBlocks; // blocks the arbiter has to rule after a dispute

    /// @notice The decoupled ruling authority (an IArbiter adapter — e.g. the UMA OOv3 adapter).
    ///         Immutable; the ONLY address allowed to call {resolveDispute}. Never an operator EOA in
    ///         production — swap in a UMA/Kleros adapter contract. See docs/ARBITRATION.md.
    address public immutable arbiter;

    uint8 public constant MAX_COMMITTEE = 7; // gas bound on the createJob loop + uint8 tallies

    /// @notice The deployment chain's slot time (seconds per block), used ONLY to bound the resolve window
    ///         from below against the arbiter's liveness. Set PER-DEPLOYMENT (Ethereum ~12s, Base ~2s) so the
    ///         guard is correct on any chain — hardcoding one chain's slot time silently under-protects on a
    ///         faster chain. A slot may be skipped (a block arrives LATER, never sooner), so
    ///         `blocks * secondsPerBlock` is a safe lower bound on the window's real-time span.
    uint64 public immutable secondsPerBlock;

    uint256 public nextJobId = 1;

    mapping(uint256 => Job)        private jobs;
    mapping(bytes32 => Commit)     private commits;     // v3 sealed bids
    mapping(uint256 => Vote)       private votes;
    mapping(uint256 => DisputeRec) private disputes;
    mapping(uint256 => address[])  private committees;  // jobId => evaluator members
    mapping(uint256 => mapping(address => bool)) private isMember;  // O(1) membership auth
    mapping(uint256 => mapping(address => bool)) private hasVoted;  // one vote per member

    // ── Events ──
    event JobCreated(
        uint256 indexed jobId,
        address indexed client,
        uint256 amount,
        bytes32 specHash,
        uint64 deadline,
        uint8 committeeSize,
        uint8 quorum
    );
    event CommitteeSet(uint256 indexed jobId, address[] evaluators, uint8 quorum);
    event JobFunded(uint256 indexed jobId, uint256 amount);
    event AcceptCommitted(bytes32 indexed commitment, uint64 commitBlock);
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

    // ── Custom errors ──
    error ZeroAddress();
    error ZeroAmount();
    error InvalidDeadline();
    error InvalidWindow();
    error InvalidSecondsPerBlock(); // deployment chain slot time must be non-zero
    error JobNotFound(uint256 jobId);
    error BadStatus(Status have, Status want);
    error NotClient(address caller);
    error NotProvider(address caller);
    error DeadlineNotReached(uint64 deadline);
    error TransferFailed();
    // sealed accept (v3)
    error ProviderIsClient();
    error ProviderIsCommitteeMember(); // a committee member cannot also win + judge the job
    error EmptyCommitment();
    error CommitmentExists();
    error CommitNotFound();
    error AlreadyRevealed();
    error TooEarlyToReveal(uint64 readyBlock);
    error RevealWindowClosed(uint64 expiryBlock);
    // committee
    error BadCommitteeSize(uint256 n);
    error CommitteeNotOdd(uint256 n);
    error BadQuorum(uint8 quorum, uint8 minMajority, uint8 n);
    error DuplicateEvaluator(address e);
    error EvaluatorIsClient();
    error NotCommitteeMember(address caller);
    error AlreadyVoted(address caller);
    error VotingClosed(uint64 deadlineBlock);
    error VotingStillOpen(uint64 deadlineBlock);
    // dispute
    error DisputeWindowOpen(uint64 windowEnd);
    error DisputeWindowClosed(uint64 windowEnd);
    error NotLosingParty(address caller);
    error NotArbiter(address caller);
    error ResolveWindowOpen(uint64 resolveEnd);
    error ResolveWindowTooShort(); // resolve window would preempt the arbiter's liveness

    constructor(
        address token_,
        uint64 revealDelayBlocks_,
        uint64 revealWindowBlocks_,
        uint64 votingWindowBlocks_,
        uint64 disputeWindowBlocks_,
        uint64 disputeResolveWindowBlocks_,
        address arbiter_,
        uint64 secondsPerBlock_
    ) {
        if (token_ == address(0) || arbiter_ == address(0)) revert ZeroAddress();
        if (revealDelayBlocks_ == 0 || revealWindowBlocks_ == 0) revert InvalidWindow();
        if (votingWindowBlocks_ == 0 || disputeWindowBlocks_ == 0 || disputeResolveWindowBlocks_ == 0)
            revert InvalidWindow();
        if (secondsPerBlock_ == 0) revert InvalidSecondsPerBlock();
        token = IERC20(token_);
        revealDelayBlocks = revealDelayBlocks_;
        revealWindowBlocks = revealWindowBlocks_;
        votingWindowBlocks = votingWindowBlocks_;
        disputeWindowBlocks = disputeWindowBlocks_;
        disputeResolveWindowBlocks = disputeResolveWindowBlocks_;
        arbiter = arbiter_;
        secondsPerBlock = secondsPerBlock_;

        // Couple the resolve-timeout window to the arbiter's liveness. {resolveTimeout} is an anti-freeze
        // backstop that enacts the committee's ORIGINAL tentative outcome once the window elapses — but if
        // that window were shorter (in real time) than the arbiter's optimistic liveness, a slow-but-honest
        // ruling could be silently preempted and every staked dispute discarded. If the arbiter exposes a
        // liveness() (the UMA adapter does), require the window to span at least that liveness so the arbiter
        // always gets to rule first. Adapters without a liveness (synchronous rulings) skip this guard.
        try IArbiterLiveness(arbiter_).liveness() returns (uint64 lv) {
            if (lv != 0 && uint256(disputeResolveWindowBlocks_) * secondsPerBlock_ < lv) {
                revert ResolveWindowTooShort();
            }
        } catch {}
    }

    /// @notice Client posts an OPEN job naming an evaluator COMMITTEE (odd N) + a strict-majority quorum.
    /// @dev    Does NOT move funds — call {fund} after approving USDC. Committee members must be distinct
    ///         and none may be the client (no self-judging).
    function createJob(
        address[] calldata evaluators,
        uint8 quorum,
        uint256 amount,
        bytes32 specHash,
        uint64 deadline
    ) external returns (uint256 jobId) {
        uint256 n = evaluators.length;
        if (amount == 0) revert ZeroAmount();
        if (deadline <= block.timestamp) revert InvalidDeadline();
        if (n == 0 || n > MAX_COMMITTEE) revert BadCommitteeSize(n);
        if (n % 2 == 0) revert CommitteeNotOdd(n);
        uint8 majority = uint8(n / 2 + 1);
        if (quorum < majority || quorum > n) revert BadQuorum(quorum, majority, uint8(n));

        jobId = nextJobId++;
        Job storage job = jobs[jobId];
        job.client = msg.sender;
        job.amount = amount;
        job.specHash = specHash;
        job.deadline = deadline;
        job.status = Status.Open;
        job.committeeSize = uint8(n);
        job.quorum = quorum;

        for (uint256 i; i < n; ++i) {
            address e = evaluators[i];
            if (e == address(0)) revert ZeroAddress();
            if (e == msg.sender) revert EvaluatorIsClient();
            if (isMember[jobId][e]) revert DuplicateEvaluator(e);
            isMember[jobId][e] = true;
            committees[jobId].push(e);
        }
        emit JobCreated(jobId, msg.sender, amount, specHash, deadline, uint8(n), quorum);
        emit CommitteeSet(jobId, evaluators, quorum);
    }

    /// @notice Client escrows the job amount in USDC (requires prior ERC-20 approval).
    function fund(uint256 jobId) external {
        Job storage job = _get(jobId);
        if (msg.sender != job.client) revert NotClient(msg.sender);
        if (job.status != Status.Open) revert BadStatus(job.status, Status.Open);

        job.status = Status.Funded;
        _safeTransferFrom(msg.sender, address(this), job.amount);
        emit JobFunded(jobId, job.amount);
    }

    // ── sealed commit-reveal accept (v3, carried) ──

    /// @notice Step 1 of the sealed accept race: publish a hidden bid (opaque hash, no jobId leaked).
    function commitAccept(bytes32 commitment) external {
        if (commitment == bytes32(0)) revert EmptyCommitment();
        Commit storage c = commits[commitment];
        if (c.revealed) revert AlreadyRevealed();
        if (c.commitBlock != 0) revert CommitmentExists();

        c.commitBlock = uint64(block.number);
        emit AcceptCommitted(commitment, uint64(block.number));
    }

    /// @notice Step 2: open the bid and claim the job; first valid reveal wins.
    function revealAccept(uint256 jobId, bytes32 salt) external {
        Job storage job = _get(jobId);
        if (job.status != Status.Funded) revert BadStatus(job.status, Status.Funded);
        if (msg.sender == job.client) revert ProviderIsClient();
        if (isMember[jobId][msg.sender]) revert ProviderIsCommitteeMember();

        bytes32 commitment = keccak256(abi.encode(jobId, msg.sender, salt));
        Commit storage c = commits[commitment];
        if (c.commitBlock == 0) revert CommitNotFound();
        if (c.revealed) revert AlreadyRevealed();

        uint64 ready = c.commitBlock + revealDelayBlocks;
        uint64 expiry = ready + revealWindowBlocks;
        if (block.number < ready) revert TooEarlyToReveal(ready);
        if (block.number > expiry) revert RevealWindowClosed(expiry);

        c.revealed = true;
        job.provider = msg.sender;
        job.status = Status.Accepted;
        emit JobAccepted(jobId, msg.sender);
    }

    /// @notice Provider submits the deliverable's content hash + Irys id, and arms the voting window.
    function submitWork(uint256 jobId, bytes32 deliverableHash, string calldata irysId) external {
        Job storage job = _get(jobId);
        if (msg.sender != job.provider) revert NotProvider(msg.sender);
        if (job.status != Status.Accepted) revert BadStatus(job.status, Status.Accepted);

        job.deliverableHash = deliverableHash;
        job.irysId = irysId;
        job.status = Status.Submitted;
        votes[jobId].votingDeadlineBlock = uint64(block.number) + votingWindowBlocks;
        emit WorkSubmitted(jobId, deliverableHash, irysId);
    }

    // ── committee voting ──

    /// @notice A committee member casts a single approve/reject vote. Reaching the quorum on either
    ///         side enters {Status.Resolved} with a TENTATIVE outcome — no funds move here.
    function castVote(uint256 jobId, bool approve) external {
        Job storage job = _get(jobId);
        if (job.status != Status.Submitted) revert BadStatus(job.status, Status.Submitted);
        if (!isMember[jobId][msg.sender]) revert NotCommitteeMember(msg.sender);
        if (hasVoted[jobId][msg.sender]) revert AlreadyVoted(msg.sender);
        Vote storage v = votes[jobId];
        if (block.number > v.votingDeadlineBlock) revert VotingClosed(v.votingDeadlineBlock);

        hasVoted[jobId][msg.sender] = true; // EFFECTS first
        uint8 q = job.quorum;
        if (approve) {
            uint8 c = ++v.approveCount;
            emit VoteCast(jobId, msg.sender, true, c);
            if (c >= q) _resolve(jobId, job, v, true);
        } else {
            uint8 c = ++v.rejectCount;
            emit VoteCast(jobId, msg.sender, false, c);
            if (c >= q) _resolve(jobId, job, v, false);
        }
    }

    function _resolve(uint256 jobId, Job storage job, Vote storage v, bool payout) private {
        job.status = Status.Resolved; // tentative — NO transfer
        v.tentativePayout = payout;
        v.resolvedBlock = uint64(block.number);
        emit JobResolved(jobId, payout, v.approveCount, v.rejectCount);
    }

    /// @notice If neither side reached quorum by the voting deadline, anyone may force a tentative
    ///         REFUND (client-favorable: the provider only earns by convincing a majority). Prevents a
    ///         stalled/abstaining committee from freezing funds. The provider may still {dispute}.
    function forceResolve(uint256 jobId) external {
        Job storage job = _get(jobId);
        if (job.status != Status.Submitted) revert BadStatus(job.status, Status.Submitted);
        Vote storage v = votes[jobId];
        if (block.number <= v.votingDeadlineBlock) revert VotingStillOpen(v.votingDeadlineBlock);
        _resolve(jobId, job, v, false);
    }

    // ── dispute window ──

    /// @notice No dispute raised → execute the committee's tentative outcome once the window closes.
    ///         Permissionless (anyone can flush). CEI-safe.
    function finalize(uint256 jobId) external {
        Job storage job = _get(jobId);
        if (job.status != Status.Resolved) revert BadStatus(job.status, Status.Resolved);
        Vote storage v = votes[jobId];
        uint64 windowEnd = v.resolvedBlock + disputeWindowBlocks;
        if (block.number <= windowEnd) revert DisputeWindowOpen(windowEnd);
        _execute(jobId, job, v.tentativePayout);
    }

    /// @notice The LOSING side stakes a bond (at the arbiter adapter) to escalate the tentative outcome
    ///         to the decoupled arbiter. Must be within the dispute window. The escrow holds no bond —
    ///         the stake is the arbiter's (e.g. UMA assertion bond); the disputer must have approved the
    ///         arbiter for that bond beforehand.
    function dispute(uint256 jobId) external {
        Job storage job = _get(jobId);
        if (job.status != Status.Resolved) revert BadStatus(job.status, Status.Resolved);
        Vote storage v = votes[jobId];
        uint64 windowEnd = v.resolvedBlock + disputeWindowBlocks;
        if (block.number > windowEnd) revert DisputeWindowClosed(windowEnd);

        address loser = v.tentativePayout ? job.client : job.provider;
        if (msg.sender != loser) revert NotLosingParty(msg.sender);

        DisputeRec storage d = disputes[jobId];
        d.disputer = msg.sender;          // EFFECTS
        d.disputeBlock = uint64(block.number);
        job.status = Status.Disputed;
        emit JobDisputed(jobId, msg.sender, v.tentativePayout);
        // INTERACTION: hand off to the decoupled arbiter (it pulls the bond + opens the oracle case).
        IArbiter(arbiter).openDispute(jobId, v.tentativePayout, msg.sender);
    }

    /// @notice FINAL ruling from the decoupled arbiter (e.g. the UMA OOv3 adapter on assertion
    ///         settlement). Arbiter-only — no admin EOA path. Executes payout or refund.
    function resolveDispute(uint256 jobId, bool payProvider) external {
        if (msg.sender != arbiter) revert NotArbiter(msg.sender);
        Job storage job = _get(jobId);
        if (job.status != Status.Disputed) revert BadStatus(job.status, Status.Disputed);
        emit DisputeResolved(jobId, payProvider);
        _execute(jobId, job, payProvider);
    }

    /// @notice Anti-freeze backstop: if the arbiter never rules within its window, anyone may flush the
    ///         job by executing the committee's ORIGINAL tentative outcome. This is NOT an arbitrary
    ///         ruling — it only ever enacts what the committee already decided, so it is not a
    ///         centralization vector. Bond recovery is handled at the arbiter adapter (UMA liveness).
    /// @dev    The constructor guarantees `disputeResolveWindowBlocks` spans at least the arbiter's liveness
    ///         (see the IArbiterLiveness coupling), so this can never preempt an honest optimistic ruling —
    ///         by the time it is callable, the arbiter's assertion is settle-able and would have resolved.
    function resolveTimeout(uint256 jobId) external {
        Job storage job = _get(jobId);
        if (job.status != Status.Disputed) revert BadStatus(job.status, Status.Disputed);
        DisputeRec storage d = disputes[jobId];
        uint64 resolveEnd = d.disputeBlock + disputeResolveWindowBlocks;
        if (block.number <= resolveEnd) revert ResolveWindowOpen(resolveEnd);
        bool tentativePayout = votes[jobId].tentativePayout;
        emit DisputeTimedOut(jobId, tentativePayout);
        _execute(jobId, job, tentativePayout);
    }

    /// @notice Client reclaims escrowed funds after the deadline if the job never reached settlement.
    /// @dev    Allowed only from Funded/Accepted — NOT Submitted. Once work is Submitted the deliverable is
    ///         on-chain and the committee owns the outcome: a client must not be able to set a short deadline,
    ///         let the provider deliver, then unilaterally refund itself and keep the work for free. A stalled
    ///         Submitted job is instead flushed permissionlessly by {forceResolve} after the voting deadline
    ///         (→ tentative refund), so funds are never strandable. Resolved/Disputed have their own
    ///         finalize/timeout exits. Outstanding commitments/votes are inert once the status moves.
    function claimRefund(uint256 jobId) external {
        Job storage job = _get(jobId);
        if (msg.sender != job.client) revert NotClient(msg.sender);
        if (job.status != Status.Funded && job.status != Status.Accepted) {
            revert BadStatus(job.status, Status.Funded);
        }
        if (block.timestamp < job.deadline) revert DeadlineNotReached(job.deadline);

        job.status = Status.Refunded;
        _safeTransfer(job.client, job.amount);
        emit RefundClaimed(jobId, job.client, job.amount);
    }

    // ── settlement execution (single money-moving primitive; CEI: status before transfer) ──

    function _execute(uint256 jobId, Job storage job, bool payProvider) private {
        if (payProvider) {
            job.status = Status.Completed;
            _safeTransfer(job.provider, job.amount);
            emit JobCompleted(jobId, job.provider, job.amount);
        } else {
            job.status = Status.Rejected;
            _safeTransfer(job.client, job.amount);
            emit JobRejected(jobId, job.client, job.amount);
        }
    }

    // ── Views ──

    function getJob(uint256 jobId) external view returns (Job memory) {
        return _get(jobId);
    }

    function getCommittee(uint256 jobId) external view returns (address[] memory) {
        return committees[jobId];
    }

    function getVote(uint256 jobId)
        external
        view
        returns (uint8 approveCount, uint8 rejectCount, uint64 votingDeadlineBlock, bool tentativePayout, uint64 resolvedBlock)
    {
        Vote storage v = votes[jobId];
        return (v.approveCount, v.rejectCount, v.votingDeadlineBlock, v.tentativePayout, v.resolvedBlock);
    }

    function getDispute(uint256 jobId) external view returns (address disputer, uint64 disputeBlock) {
        DisputeRec storage d = disputes[jobId];
        return (d.disputer, d.disputeBlock);
    }

    function hasMemberVoted(uint256 jobId, address member) external view returns (bool) {
        return hasVoted[jobId][member];
    }

    function commitInfo(bytes32 commitment) external view returns (uint64 commitBlock, bool revealed) {
        Commit storage c = commits[commitment];
        return (c.commitBlock, c.revealed);
    }

    // ── Internal ──

    function _get(uint256 jobId) private view returns (Job storage job) {
        job = jobs[jobId];
        if (job.status == Status.None) revert JobNotFound(jobId);
    }

    function _safeTransfer(address to, uint256 amount) private {
        _safeCall(abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
    }

    function _safeTransferFrom(address from, address to, uint256 amount) private {
        _safeCall(abi.encodeWithSelector(IERC20.transferFrom.selector, from, to, amount));
    }

    function _safeCall(bytes memory data) private {
        (bool ok, bytes memory ret) = address(token).call(data);
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TransferFailed();
    }
}
