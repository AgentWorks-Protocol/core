import "../dashboard.css";
import { Shell } from "../../components/dashboard/Shell";

export const dynamic = "force-dynamic";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="dp">
      <Shell />
      <main className="wrap main">{children}</main>
    </div>
  );
}
