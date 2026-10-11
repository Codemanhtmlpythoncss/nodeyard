
(function(){
  var DAYS=['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
  var MON=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  function tick(){var d=new Date(),p=function(n){return (n<10?'0':'')+n};
    document.getElementById('clock').textContent=p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
    document.getElementById('date').textContent=DAYS[d.getDay()]+' · '+d.getDate()+' '+MON[d.getMonth()]+' '+d.getFullYear();}
  tick();setInterval(tick,1000);
})();
