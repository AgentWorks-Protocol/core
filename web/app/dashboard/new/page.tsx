import { PostJob } from "../../../components/dashboard/PostJob";

export const dynamic = "force-dynamic";

export default function NewJobPage() {
  return (
    <>
      <div className="head">
        <h1>Post a job</h1>
        <p>
          Open and fund a job on the marketplace with your <strong>own</strong> Cobo Agentic Wallet. The platform
          encodes the <code>createJob</code>/<code>approve</code>/<code>fund</code> calldata; you sign it — no keys
          ever leave your side. Any registered provider can then claim it, and a committee of registered evaluators
          settles it on-chain.
        </p>
      </div>
      <PostJob />
    </>
  );
}
