import { Link } from "react-router-dom";
import { Empty } from "../components/ui";

export default function NotFound() {
  return (
    <Empty title="No such page">
      <Link className="text-accent underline" to="/">Back to command</Link>
    </Empty>
  );
}
