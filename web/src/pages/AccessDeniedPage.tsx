import { ShieldAlert } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useNavigate } from "react-router";

export default function AccessDeniedPage() {
  const navigate = useNavigate();
  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 rounded-lg border border-current/15 bg-background/60 p-6 text-text-primary">
      <div className="flex items-center gap-3">
        <ShieldAlert className="h-6 w-6 text-amber-300" />
        <h1 className="font-sans text-display text-xl tracking-[0.08em]">Access denied</h1>
      </div>
      <p className="text-sm text-text-secondary">
        Your dashboard governance policy does not grant access to this page or action.
      </p>
      <div>
        <Button onClick={() => navigate("/sessions")}>
          Back to sessions
        </Button>
      </div>
    </div>
  );
}
