package place.poster.app.sms;

import java.util.*;

/** Pure presentation of English SMS fallback reactions. Never changes or sends provider rows.
 * Callers must supply strict one-to-one thread identity, actual sender identity, namespaced ids,
 * and eligibility only for received/successfully sent rows. Incomplete history stays ordinary text. */
public final class SmsReactions {
    private SmsReactions() { }
    private static final String[][] FORMS = {
        {"heart","❤️","Loved","Removed a heart from"},
        {"like","👍","Liked","Removed a like from"},
        {"dislike","👎","Disliked","Removed a dislike from"},
        {"laugh","😂","Laughed at","Removed a laugh from"},
        {"emphasize","‼️","Emphasized","Removed an emphasis from"},
        {"question","❓","Questioned","Removed a question mark from"}
    };
    public static final class Parsed {
        public final String kind, emoji, operation, text;
        Parsed(String[] form, String op, String text) {
            kind=form[0]; emoji=form[1]; operation=op; this.text=text;
        }
    }
    public static Parsed parse(String body) {
        if(body==null) return null;
        for(String[] form:FORMS) for(int i=2;i<4;i++) {
            String prefix=form[i]+" "; if(!body.startsWith(prefix)) continue;
            String q=body.substring(prefix.length()); if(q.length()<3) continue;
            char first=q.charAt(0), close=first=='“'?'”':first=='"'?'"':0;
            if(close==0 || q.charAt(q.length()-1)!=close) continue;
            return new Parsed(form,i==2?"add":"remove",q.substring(1,q.length()-1));
        }
        return null;
    }
    public static final class Message {
        public final String id, thread, actor, body;
        public final long date;
        public final boolean incoming, eligible, group;
        public Message(String id,String thread,String actor,String body,long date,
                       boolean incoming,boolean eligible,boolean group) {
            this.id=id;this.thread=thread;this.actor=actor;this.body=body;this.date=date;
            this.incoming=incoming;this.eligible=eligible;this.group=group;
        }
        private List<Object> fields(){return Arrays.asList(id,thread,actor,body,date,incoming,eligible,group);}
    }
    public static final class Chip {
        public final String target, actor, kind, emoji, sourceId;
        Chip(Operation op){target=op.target;actor=op.row.actor;kind=op.parsed.kind;
            emoji=op.parsed.emoji;sourceId=op.row.id;}
    }
    public static final class Projection {
        public final Map<String,List<Chip>> chipsByTarget=new LinkedHashMap<>();
        public final Set<String> consumedIds=new LinkedHashSet<>();
    }
    private static final class Operation {
        final Message row;final Parsed parsed;final String target;
        Operation(Message row,Parsed parsed,String target){this.row=row;this.parsed=parsed;this.target=target;}
        List<String> key(){return Arrays.asList(row.thread,target,row.actor);}
        List<Object> timeKey(){return Arrays.asList(key(),row.date);}
    }
    private static boolean present(String s){return s!=null&&!s.isEmpty();}
    public static Projection project(List<Message> rows,boolean historyComplete){
        Projection result=new Projection();if(!historyComplete||rows==null)return result;
        Map<String,Message> unique=new LinkedHashMap<>();Set<String> conflicts=new HashSet<>();
        for(Message row:rows){
            if(row==null||!present(row.id))continue;
            Message prior=unique.get(row.id);
            if(prior!=null&&!prior.fields().equals(row.fields()))conflicts.add(row.id);
            else if(prior==null)unique.put(row.id,row);
        }
        // Conflicting identities make target uniqueness unknowable in this snapshot.
        if(!conflicts.isEmpty())return result;
        List<Message> messages=new ArrayList<>();
        for(Message row:unique.values())if(!conflicts.contains(row.id)&&present(row.thread)
                &&present(row.actor)&&row.body!=null&&row.date>=0&&row.date<=9007199254740991L
                &&row.eligible&&!row.group)messages.add(row);
        List<Operation> ops=new ArrayList<>();
        for(Message row:messages){
            Parsed parsed=parse(row.body);if(parsed==null)continue;
            Message target=null;int count=0;
            for(Message m:messages)if(!m.id.equals(row.id)&&m.thread.equals(row.thread)
                    &&m.incoming!=row.incoming&&m.date<row.date&&m.body.equals(parsed.text)&&parse(m.body)==null){
                target=m;count++;
            }
            if(count==1)ops.add(new Operation(row,parsed,target.id));
        }
        Map<List<Object>,Integer> ties=new HashMap<>();
        for(Operation op:ops)ties.put(op.timeKey(),ties.getOrDefault(op.timeKey(),0)+1);
        Collections.sort(ops,Comparator.comparingLong(op->op.row.date));
        Map<List<String>,Chip> active=new LinkedHashMap<>();
        for(Operation op:ops){
            if(ties.get(op.timeKey())>1)continue;
            List<String> key=op.key();Chip previous=active.get(key);
            if(op.parsed.operation.equals("remove")){
                if(previous==null||!previous.kind.equals(op.parsed.kind))continue;
                active.remove(key);
            }else active.put(key,new Chip(op));
            result.consumedIds.add(op.row.id);
        }
        for(Chip chip:active.values())result.chipsByTarget.computeIfAbsent(chip.target,k->new ArrayList<>()).add(chip);
        return result;
    }
}
